import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { api } from "../api";
import { mapHistoryItems, mergeHistoryWithLiveMessages, normalizeTurnFileChanges, preserveLocalProcessDetails, settleFollowupAnswer, toolExitUnconfirmed, type ToolResultMetadata, type BackendHistoryItem } from "../lib/historyMap";
import { backendSecondsToMs } from "../lib/backendTime";
import { mergeStreamingText } from "../lib/agentTurnState";
import { normalizeTeamText } from "../lib/chunkNormalize";
import { reviewFromSnapshot, samePlan } from "../lib/planDecisions";
import { ChatSocket, GatewaySequenceTracker } from "../ws";
import {
  parseFeatureEventBody,
  type Bookkeeping,
  type DeltaSpan,
  type FeatureEventContext,
  type FeatureEventEffect,
  type FeatureEventRegistry,
} from "../lib/feature-event-dispatcher";
import type { Attachment, Chunk, FollowupQuestion, Mode, MsgRole, PendingMessage, PlanDecision, PlanReview, TeamExecutionTier, TodoItem, ToolCallInfo, TurnFileChangeSummary, UiMessage, UserAgentMention, WikiIngestProgress, WikiPage, SessionStatus } from "../types";

let _seq = 0;
const newRequestId = () => "web_" + Array.from(crypto.getRandomValues(new Uint32Array(4)), (n) => n.toString(16).padStart(8, "0")).join("");
const newId = () => `m${Date.now()}_${_seq++}`;
const HISTORY_LOADING_TEXT = "正在加载历史记录…";
const HISTORY_LOAD_TIMEOUT_MS = 8000;
const RECOVERY_POLL_MS = 1500;
const RECOVERY_NOTICE = "连接已恢复，正在同步会话历史…";
const RECOVERY_RUNNING_NOTICE = "回复仍在生成，完成后将同步完整记录。";
const RESYNC_REASONS = new Set(["epoch_changed", "cursor_expired", "cursor_ahead", "invalid_cursor"]);

type SessionStatusSnapshot = Awaited<ReturnType<typeof api.sessionStatus>>;
interface SessionRecovery {
  revision: number;
  inFlight: boolean;
  retryRequested: boolean;
  verifiedFollowupId?: string;
  timer?: ReturnType<typeof setTimeout>;
}

interface RecoveryHistoryLoad {
  recovery: SessionRecovery;
  revision: number;
  status: SessionStatusSnapshot;
}

function isHistoryLoadingMessage(msg: UiMessage): boolean {
  return msg.role === "assistant" && msg.text === HISTORY_LOADING_TEXT && !msg.thinking && !msg.toolCalls?.length;
}

function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timeout`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

const closedFollowupStatuses = ["expired", "cancelled", "resolved"];

function parseFollowupQuestion(body: Chunk["body"], status: string): FollowupQuestion {
  return {
    question_id: body.question_id ?? "",
    title: body.title ?? "",
    record_history: body.record_history !== false,
    status,
    note: typeof body.note === "string" ? body.note : undefined,
    origin: body.origin && typeof body.origin === "object" ? body.origin : undefined,
    questions: Array.isArray(body.questions)
      ? body.questions.map((item: any) => ({
          id: typeof item?.id === "string" ? item.id : "",
          question: typeof item?.question === "string" ? item.question : "",
          options: Array.isArray(item?.options)
            ? item.options.map((opt: any) => {
                if (opt && typeof opt === "object") {
                  const label = String(opt.label ?? opt.text ?? opt.name ?? opt.value ?? "");
                  const value = String(opt.value ?? label);
                  const rawDescription = opt.description ?? opt.desc ?? opt.detail ?? opt.details
                    ?? opt.content ?? opt.body ?? opt.explanation ?? opt.summary;
                  const description = typeof rawDescription === "string" ? rawDescription.trim() : "";
                  return description ? { label, value, description } : { label, value };
                }
                const text = String(opt ?? "");
                return { label: text, value: text };
              })
            : [],
          inputMode: typeof item?.inputMode === "string"
            ? item.inputMode
            : (typeof item?.input_mode === "string" ? item.input_mode : undefined),
          allowFreeText: item?.allowFreeText !== false && item?.allow_free_text !== false,
          multiSelect: !!item?.multiSelect,
        }))
      : [],
  };
}

function formatFollowupAnswers(
  question: FollowupQuestion | null | undefined,
  answers: { question_id: string; answers: string[] }[],
): string {
  const labelsByQuestion = new Map<string, Map<string, string>>();
  for (const item of question?.questions ?? []) {
    const labels = new Map<string, string>();
    for (const opt of item.options) {
      labels.set(opt.value, opt.description ? `${opt.label}：${opt.description}` : opt.label);
    }
    labelsByQuestion.set(item.id, labels);
  }

  return answers
    .map((item) => {
      const labels = labelsByQuestion.get(item.question_id);
      return item.answers.map((value) => labels?.get(value) ?? value).join(", ");
    })
    .filter(Boolean)
    .join("；");
}

function isPlanControlStatus(message: string): boolean {
  return (
    message.startsWith("已进入 Plan 模式") ||
    message.startsWith("已保留 Plan 模式") ||
    message.startsWith("已退出 Plan 模式")
  );
}

export function isTeamRuntimeStatus(message: string): boolean {
  return (
    message.startsWith("Team Runtime ") ||
    message.startsWith("简单消息由 Leader 直接回复") ||
    message.startsWith("Leader 正在") ||
    message.startsWith("并发派发节点") ||
    message.startsWith("派发节点") ||
    message.startsWith("完成节点")
  );
}

/** 取消后的旧请求即使迟到，也不能污染重试产生的新回合。 */
export function isSuppressedRequest(requestId: unknown, suppressed: ReadonlySet<string>): boolean {
  const normalized = String(requestId || "").trim();
  return Boolean(normalized && suppressed.has(normalized));
}

function suppressedRequestKey(sessionId: string, requestId: string): string {
  return `${sessionId}\u0000${requestId}`;
}

export function normalizeWikiCardPages(body: Record<string, unknown>): WikiPage[] {
  const raw = Array.isArray(body.pages) ? body.pages : Array.isArray(body.cards) ? body.cards : [];
  return raw as WikiPage[];
}

function normalizeTodos(raw: unknown): TodoItem[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((item): TodoItem => {
    const value = item && typeof item === "object" ? item as Record<string, unknown> : {};
    return {
      id: String(value.id ?? ""),
      content: String(value.content ?? ""),
      status: String(value.status ?? "pending"),
    };
  }).filter((item) => item.id || item.content);
}

/**
 * 决定 final 帧到来时应保留累积文本还是采用 final 文本。
 *
 * 背景：后端 `final` 只携带最后一轮 assistant 的完整文本，不是多轮回合的累积全文。
 * 若用 final 无脑覆盖，会冲掉前面轮次已流式出来的内容，表现为输出错乱/跳变。
 *
 * 策略：
 * - 累积已包含 final 文本（正常流式）→ 保留累积全文
 * - final 明显比累积更完整（单轮 delta 大量丢帧兜底）→ 采用 final
 * - 两者无明显包含关系（多轮场景 final 只带最后一轮）→ 保守保留累积
 */
export function resolveFinalText(accumulated: string, finalText: string): string {
  if (!finalText) return accumulated;
  const accTrim = accumulated.trimEnd();
  const textTrim = finalText.trimEnd();
  if (accTrim.includes(textTrim)) return accumulated;
  if (textTrim.length > accTrim.length && textTrim.includes(accTrim)) return finalText;
  if (textTrim.length >= Math.max(1, accTrim.length * 0.95)) return finalText;
  return accumulated;
}

/** 会话运行态（驱动左侧栏状态点）。 */
type PlanState = { active: boolean; review: PlanReview | null };
interface SendOptions {
  subScenario?: string;
  externalTeamId?: string;
  wikiKbId?: string;
  externalWikiReadKbId?: string;
  teamExecutionTier?: TeamExecutionTier;
  userMentions?: UserAgentMention[];
}

/** 单会话的聚合记账（不直接驱动渲染，放 ref）。 */

function fileChangeSignature(file: TurnFileChangeSummary, raw?: unknown): string {
  const base = `${file.status}|${file.added}|${file.removed}|${file.binary ? "1" : "0"}`;
  if (!raw || typeof raw !== "object") return base;
  const value = raw as Record<string, unknown>;
  return `${base}|${String(value.revision || "")}|${JSON.stringify(value.diff ?? [])}`;
}

function snapshotFileSignatures(
  files: TurnFileChangeSummary[],
  rawFiles?: unknown,
): Record<string, string> {
  const rawByPath = new Map<string, unknown>();
  if (Array.isArray(rawFiles)) {
    for (const item of rawFiles) {
      if (!item || typeof item !== "object") continue;
      const path = String((item as Record<string, unknown>).path || "").trim();
      if (path) rawByPath.set(path, item);
    }
  }
  return Object.fromEntries(
    files.map((file) => [file.path, fileChangeSignature(file, rawByPath.get(file.path))]),
  );
}

function currentTurnFileChanges(book: Bookkeeping): TurnFileChangeSummary[] {
  return book.fileChanges.filter((file) =>
    book.prevTurnFileSignature[file.path] !== book.fileChangeSignatures[file.path],
  );
}


export interface DeltaAccumulator {
  deltaSpans: DeltaSpan[];
  legacyDeltaText: string;
}

export interface AssistantTextDeltaAccumulator extends DeltaAccumulator {
  awaitingAssistantAfterTool: boolean;
}

function deltaRangeFromChunk(c: Chunk): { start: number; end: number } | null {
  const startRaw = c.body.delta_start ?? c.sequence;
  const endRaw = c.body.delta_end ?? c.sequence;
  const start = typeof startRaw === "number" ? startRaw : Number(startRaw);
  const end = typeof endRaw === "number" ? endRaw : Number(endRaw);
  if (!Number.isFinite(start) || !Number.isFinite(end) || start <= 0 || end <= 0) return null;
  return { start: Math.min(start, end), end: Math.max(start, end) };
}

function renderDeltaText(book: DeltaAccumulator): string {
  return book.legacyDeltaText + book.deltaSpans
    .slice()
    .sort((a, b) => a.start - b.start || a.end - b.end)
    .map((span) => span.text)
    .join("");
}

export function applyOrderedDelta(
  state: DeltaAccumulator,
  chunk: Pick<Chunk, "body" | "sequence">,
  text: string,
): string {
  const book = state;
  const range = deltaRangeFromChunk(chunk as Chunk);
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

export function applyAssistantTextDelta(
  state: AssistantTextDeltaAccumulator,
  chunk: Pick<Chunk, "body" | "sequence">,
  text: string,
): string {
  if (state.awaitingAssistantAfterTool) {
    state.awaitingAssistantAfterTool = false;
    resetDeltaBook(state);
  }
  return applyOrderedDelta(state, chunk, text);
}

function resetDeltaBook(book: DeltaAccumulator): void {
  book.deltaSpans = [];
  book.legacyDeltaText = "";
}

/** tool 事件体（从 Chunk.body 中取出，用于单元测试）。 */
export interface ToolChunkBody {
  phase: "generating" | "start" | "result" | "error";
  tool_call_id?: string;
  name?: string;
  ui_label?: string;
  args?: string;
  detail?: string;
  result_metadata?: ToolResultMetadata;
}

/**
 * 把单个 tool 事件应用到 toolMap。
 *
 * 两阶段语义：
 * - generating：模型正在生成参数，前端应立即渲染「准备调用」状态；
 * - start：参数已就绪，开始执行，状态切为 running；
 * - result / error：执行结束，计算 duration（从 earliest startedAt 到结果时间）。
 *
 * 如果事件顺序为 generating → start，start 会保留 generating 时记录的 startedAt，
 * 因此 duration 覆盖「模型决定调用 → 执行完成」的全过程，符合用户对耗时的感知。
 */
export function applyToolChunk(
  toolMap: Map<string, ToolCallInfo>,
  b: ToolChunkBody,
  now: number,
  fallbackId: () => string,
): Map<string, ToolCallInfo> {
  const toolCallId = b.tool_call_id || fallbackId();
  const next = new Map(toolMap);
  const existing = next.get(toolCallId);

  if (b.phase === "start") {
    next.set(toolCallId, {
      toolCallId,
      name: b.name ?? existing?.name ?? "unknown",
      uiLabel: b.ui_label ?? existing?.uiLabel ?? "",
      args: b.args ?? existing?.args ?? "",
      status: "running",
      startedAt: existing?.startedAt ?? now,
    });
    return next;
  }

  if (b.phase === "generating") {
    if (existing) {
      next.set(toolCallId, {
        ...existing,
        name: b.name ?? existing.name,
        uiLabel: b.ui_label ?? existing.uiLabel,
        args: b.args ?? existing.args,
        status: "generating",
      });
    } else {
      next.set(toolCallId, {
        toolCallId,
        name: b.name ?? "unknown",
        uiLabel: b.ui_label ?? "",
        args: b.args ?? "",
        status: "generating",
        startedAt: now,
      });
    }
    return next;
  }

  // result / error
  const exitUnconfirmed = toolExitUnconfirmed(b.result_metadata);
  const result = b.detail || (exitUnconfirmed ? "工具实际退出尚未确认。" : existing?.result ?? "");
  if (existing) {
    next.set(toolCallId, {
      ...existing,
      name: b.name ?? existing.name,
      uiLabel: b.ui_label ?? existing.uiLabel,
      args: b.args ?? existing.args,
      status: b.phase === "error" || exitUnconfirmed ? "error" : "done",
      result,
      ...(exitUnconfirmed ? { exitUnconfirmed: true } : {}),
      duration: now - existing.startedAt,
    });
  } else {
    next.set(toolCallId, {
      toolCallId,
      name: b.name ?? "unknown",
      uiLabel: b.ui_label ?? "",
      args: b.args ?? "",
      result,
      ...(exitUnconfirmed ? { exitUnconfirmed: true } : {}),
      status: b.phase === "error" || exitUnconfirmed ? "error" : "done",
      startedAt: now,
      duration: 0,
    });
  }
  return next;
}

/**
 * 管理 WebSocket 连接 + 「按会话分桶」的消息状态机。
 *
 * 每个 session_id 拥有独立的消息缓存、busy、队列提示与聚合记账。
 * WS 帧带 session_id（见后端），handler 据此路由到对应会话，
 * 因此多对话互不串台，后台会话也能边生成边写入自己的桶、切回去仍是实时流式。
 */
type MessagePatch = Partial<UiMessage> | ((message: UiMessage) => Partial<UiMessage>);

interface TurnOutput {
  append(role: "assistant" | "status", text: string, extra: Partial<UiMessage>): string;
  patch(id: string, patch: MessagePatch): void;
  setTodos(todos: TodoItem[]): void;
}

function createTurnBook(
  turnStartedAt: number | null = null,
  prevTurnFileSignature: Record<string, string> = {},
): Bookkeeping {
  return {
    toolMap: new Map(),
    assistantId: null,
    turnStartedAt,
    awaitingAssistantAfterTool: false,
    deltaSpans: [],
    legacyDeltaText: "",
    hadTeamInternal: false,
    fileChanges: [],
    fileChangeSignatures: {},
    prevTurnFileSignature,
  };
}

function startTurn(book: Bookkeeping, now: number): number {
  return book.turnStartedAt ??= now;
}

function startAssistantSegment(book: Bookkeeping): void {
  book.assistantId = null;
  book.toolMap.clear();
  book.awaitingAssistantAfterTool = false;
  resetDeltaBook(book);
}

function showsTurnOutput(chunk: Chunk): boolean {
  return chunk.kind === "delta" || chunk.kind === "thinking" || chunk.kind === "tool" || chunk.kind === "status";
}

/** Project display data for both live frames and a temporary active-turn snapshot. */
function applyTurnOutput(chunk: Chunk, book: Bookkeeping, output: TurnOutput, now: number): boolean {
  const { kind, body } = chunk;
  if (kind === "delta") {
    const turnStartedAt = startTurn(book, now);
    if (book.awaitingAssistantAfterTool) startAssistantSegment(book);
    const text = applyOrderedDelta(book, chunk, normalizeTeamText(body.text));
    if (book.assistantId) {
      output.patch(book.assistantId, (message) => ({ text, turnStartedAt: message.turnStartedAt ?? turnStartedAt }));
    } else {
      book.assistantId = output.append("assistant", text, { turnStartedAt });
    }
  } else if (kind === "thinking") {
    const thinking = body.text ?? "";
    if (!thinking.trim()) return true;
    if (book.awaitingAssistantAfterTool) startAssistantSegment(book);
    if (book.assistantId) {
      output.patch(book.assistantId, (message) => ({ thinking: mergeStreamingText(message.thinking, thinking) }));
    } else {
      book.assistantId = output.append("assistant", "", { turnStartedAt: startTurn(book, now), thinking });
    }
  } else if (kind === "tool") {
    const turnStartedAt = startTurn(book, now);
    book.toolMap = applyToolChunk(book.toolMap, body as ToolChunkBody, now, newId);
    const toolCalls = Array.from(book.toolMap.values());
    if (book.assistantId) {
      output.patch(book.assistantId, { turnStartedAt, toolCalls });
    } else {
      book.assistantId = output.append("assistant", "", { turnStartedAt, toolCalls });
    }
    if (body.phase !== "start") book.awaitingAssistantAfterTool = true;
  } else if (kind === "file_changes") {
    book.fileChanges = normalizeTurnFileChanges(body.files) ?? [];
    book.fileChangeSignatures = snapshotFileSignatures(book.fileChanges, body.files);
  } else if (kind === "todo_updated") {
    output.setTodos(normalizeTodos(body.todos));
  } else if (kind === "status") {
    const message = body.message ?? "";
    if (body.activity === "context_compaction" || message.includes("排队")
      || isPlanControlStatus(message) || isTeamRuntimeStatus(message)) return false;
    output.append("status", message, { turnStartedAt: startTurn(book, now) });
  } else {
    return false;
  }
  return true;
}

const SNAPSHOT_FRAME_KINDS = new Set<Chunk["kind"]>([
  "delta", "thinking", "tool", "status", "todo_updated", "file_changes",
]);

interface ActiveTurnSnapshot {
  request_id: string;
  started_at: number;
  history: {
    items: BackendHistoryItem[];
    has_more: boolean;
    next_before: string | null;
  };
  frames: Chunk[];
}

function parseActiveTurnSnapshot(value: ActiveTurnSnapshot | undefined, boundary: Chunk): ActiveTurnSnapshot | null {
  if (!value || typeof value.request_id !== "string" || !value.request_id
    || !Number.isFinite(value.started_at) || value.started_at <= 0
    || !Array.isArray(value.history?.items) || typeof value.history.has_more !== "boolean"
    || !(value.history.next_before === null || typeof value.history.next_before === "string")
    || !Array.isArray(value.frames)) return null;

  let sequence = 0;
  for (const frame of value.frames) {
    if (!frame || !SNAPSHOT_FRAME_KINDS.has(frame.kind) || !frame.body || frame.is_final
      || frame.session_id !== boundary.session_id || frame.gateway_epoch !== boundary.gateway_epoch
      || frame.request_id !== value.request_id || typeof frame.gateway_sequence !== "number"
      || !Number.isSafeInteger(frame.gateway_sequence) || frame.gateway_sequence <= sequence
      || frame.gateway_sequence > (boundary.gateway_sequence ?? -1)) return null;
    if ((frame.kind === "thinking" && frame.body.text != null && typeof frame.body.text !== "string")
      || (frame.kind === "status" && frame.body.message != null && typeof frame.body.message !== "string")) return null;
    sequence = frame.gateway_sequence;
  }
  if (value.history.items.some((item) => item == null
    || typeof item.role !== "string" || typeof item.content !== "string"
    || (item.tool_calls != null && (!Array.isArray(item.tool_calls) || item.tool_calls.some((tool) => !tool))))) return null;
  return value;
}

function projectActiveTurn(
  snapshot: ActiveTurnSnapshot,
  prevTurnFileSignature: Record<string, string> | undefined,
) {
  const startedAt = backendSecondsToMs(snapshot.started_at)!;
  const book = createTurnBook(startedAt, prevTurnFileSignature);
  const messages: UiMessage[] = [];
  const indexes = new Map<string, number>();
  let todos: TodoItem[] | undefined;
  let compaction = false;
  let queueHint = "";
  let queued = false;
  const output: TurnOutput = {
    append(role, text, extra) {
      const id = newId();
      indexes.set(id, messages.length);
      messages.push({ id, role, text, ...extra });
      return id;
    },
    patch(id, patch) {
      const index = indexes.get(id)!;
      const message = messages[index];
      messages[index] = { ...message, ...(typeof patch === "function" ? patch(message) : patch) };
    },
    setTodos(value) { todos = value; },
  };
  for (const chunk of snapshot.frames) {
    if (applyTurnOutput(chunk, book, output, startedAt)) {
      if (showsTurnOutput(chunk)) {
        queueHint = "";
        queued = false;
      }
    } else if (chunk.kind === "status") {
      const message = chunk.body.message ?? "";
      if (chunk.body.activity === "context_compaction") {
        compaction = chunk.body.active === true;
      } else if (message.includes("排队") || isTeamRuntimeStatus(message)) {
        queueHint = message;
        queued = message.includes("排队");
      }
    }
  }
  return { book, messages, todos, compaction, queueHint, queued };
}

function matchesSnapshotHistory(canonical: UiMessage, local: UiMessage): boolean {
  if (canonical.id.startsWith("followup:") || local.id.startsWith("followup:")) return canonical.id === local.id;
  if (canonical.role !== local.role || canonical.text !== local.text) return false;
  if (canonical.requestId && local.requestId) return canonical.requestId === local.requestId;
  return canonical.timestamp == null || local.timestamp == null || canonical.timestamp === local.timestamp;
}

function mergeActiveSnapshotHistory(history: UiMessage[], local: UiMessage[], hasMore: boolean): UiMessage[] {
  // Both sources are chronological. Align their latest user messages before
  // transferring local attachment data, which the history endpoint omits.
  const localUsers = local.map((message, index) => ({ message, index }))
    .filter(({ message }) => message.role === "user");
  let cursor = localUsers.length - 1;
  let oldestMatch: number | undefined;
  const merged = history.slice();
  for (let index = merged.length - 1; index >= 0; index -= 1) {
    const canonical = merged[index];
    if (canonical.role !== "user") continue;
    let match = cursor;
    while (match >= 0 && !matchesSnapshotHistory(canonical, localUsers[match].message)) match -= 1;
    if (match < 0) continue;
    const { message, index: localIndex } = localUsers[match];
    merged[index] = { ...canonical, id: message.id, attachments: message.attachments };
    cursor = match - 1;
    if (index === 0 && !localUsers.slice(0, match).some((candidate) => matchesSnapshotHistory(canonical, candidate.message))) {
      oldestMatch = localIndex;
    }
  }
  return hasMore && oldestMatch != null ? [...local.slice(0, oldestMatch), ...merged] : merged;
}

export function useChat(
  currentSessionId: string,
  onAfterFinal: () => void,
  featureEventRegistry: FeatureEventRegistry,
) {
  const [messagesMap, setMessagesMap] = useState<Record<string, UiMessage[]>>({});
  const [busyMap, setBusyMap] = useState<Record<string, boolean>>({});
  const [queueMap, setQueueMap] = useState<Record<string, string>>({});
  const [recoveryNotices, setRecoveryNotices] = useState<Record<string, string>>({});
  const [statusMap, setStatusMap] = useState<Record<string, SessionStatus>>({});
  const [pendingQueueMap, setPendingQueueMap] = useState<Record<string, PendingMessage[]>>({});
  const [todoMap, setTodoMap] = useState<Record<string, TodoItem[]>>({});
  const [compactionMap, setCompactionMap] = useState<Record<string, boolean>>({});
  // Plan 模式：每会话 { active 是否处于只读 plan 态; review 待审批的计划 }
  const [planMap, setPlanMap] = useState<Record<string, PlanState>>({});
  // 追问选择框：每会话当前展示的问题
  const [followupMap, setFollowupMap] = useState<Record<string, FollowupQuestion | null>>({});
  // Wiki ingest 进度：按 source_id 维护每个上传任务的最新阶段进度。
  // 之前按 session_id 存储会导致多文件同时上传时互相覆盖、中间阶段丢失。
  const [wikiProgressMap, setWikiProgressMap] = useState<Record<string, WikiIngestProgress>>({});
  const [connected, setConnected] = useState(false);

  const sockRef = useRef<ChatSocket | null>(null);
  const compactionStartedAtRef = useRef<Record<string, number>>({});
  const compactionTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const afterFinalRef = useRef(onAfterFinal);
  afterFinalRef.current = onAfterFinal;
  const featureEventRegistryRef = useRef(featureEventRegistry);
  featureEventRegistryRef.current = featureEventRegistry;

  // task 流式事件一轮可能来几十次：全量刷新（会话列表 + /api/tasks）做 1s 节流，
  // 避免每个 task 事件都各打一次请求（404 场景下会形成风暴）
  const afterFinalThrottleRef = useRef<{ last: number; timer: ReturnType<typeof setTimeout> | undefined }>({ last: 0, timer: undefined });
  const fireAfterFinalThrottled = useCallback(() => {
    const state = afterFinalThrottleRef.current;
    const now = Date.now();
    const elapsed = now - state.last;
    if (state.timer !== undefined) {
      clearTimeout(state.timer);
      state.timer = undefined;
    }
    if (elapsed >= 1000) {
      state.last = now;
      afterFinalRef.current();
    } else {
      state.timer = setTimeout(() => {
        state.timer = undefined;
        state.last = Date.now();
        afterFinalRef.current();
      }, 1000 - elapsed);
    }
  }, []);

  // 当前会话 id（供 chunk 缺 session_id 时容错回退）
  const currentSidRef = useRef(currentSessionId);
  currentSidRef.current = currentSessionId;

  // currentSessionId 变化时（例如切换会话、新建会话），立即订阅新 session，
  // 确保 Wiki 上传进度等后台帧能正确路由到当前会话。
  useEffect(() => {
    if (!currentSessionId) return;
    subscribedSessionsRef.current.add(currentSessionId);
    syncSubscriptions(Array.from(subscribedSessionsRef.current));
  }, [currentSessionId]);

  // per-session 聚合记账 + busy 镜像（同步读取，避免闭包拿到旧 state）
  const bookRef = useRef<Map<string, Bookkeeping>>(new Map());
  const busyRef = useRef<Record<string, boolean>>({});
  const pendingQueueRef = useRef<Record<string, PendingMessage[]>>({});
  const currentReviews = useRef(new Map<string, { review: PlanReview; active: boolean }>());
  const planMapRef = useRef<Record<string, PlanState>>({});
  const followupMapRef = useRef<Record<string, FollowupQuestion | null>>({});
  // wikiProgressMap 的 ref 镜像，key 同样是 source_id。
  const wikiProgressRef = useRef<Record<string, WikiIngestProgress>>({});
  const subscribedSessionsRef = useRef<Set<string>>(new Set());
  const suppressChunksRef = useRef<Set<string>>(new Set());
  const suppressedRequestIdsRef = useRef<Set<string>>(new Set());
  const loadedSessionsRef = useRef<Set<string>>(new Set());
  const gatewaySequences = useRef(new GatewaySequenceTracker());
  const recoveries = useRef(new Map<string, SessionRecovery>());
  const historyLoads = useRef(new Map<string, symbol>());
  const snapshotBooks = useRef(new WeakSet<Bookkeeping>());
  const transportOpen = useRef(false);
  const activeRequests = useRef(new Map<string, string>());
  const stopNotices = useRef(new Map<string, { id: string; uncertainTool: boolean; terminal: boolean }>());

  const bookFor = (sid: string): Bookkeeping => {
    let b = bookRef.current.get(sid);
    if (!b) {
      b = createTurnBook();
      bookRef.current.set(sid, b);
    }
    return b;
  };

  const startLocalTurn = (sid: string): number => {
    return startTurn(bookFor(sid), Date.now());
  };

  const localTurnTiming = (sid: string) => {
    const turnStartedAt = startLocalTurn(sid);
    return {
      turnStartedAt,
      turnDurationMs: Math.max(0, Date.now() - turnStartedAt),
    };
  };

  const setBusy = (sid: string, val: boolean) => {
    busyRef.current[sid] = val;
    setBusyMap((prev) => ({ ...prev, [sid]: val }));
  };
  const setStatus = (sid: string, s: SessionStatus) =>
    setStatusMap((prev) => ({ ...prev, [sid]: s }));
  const setQueue = (sid: string, hint: string) =>
    setQueueMap((prev) => ({ ...prev, [sid]: hint }));
  const updatePlanMap = (
    updater: (prev: Record<string, PlanState>) => Record<string, PlanState>,
  ) => {
    setPlanMap((prev) => {
      const next = updater(prev);
      planMapRef.current = next;
      return next;
    });
  };
  const setPlan = (sid: string, patch: Partial<PlanState>) =>
    updatePlanMap((prev) => {
      const cur = prev[sid] ?? { active: false, review: null };
      return { ...prev, [sid]: { ...cur, ...patch } };
    });

  const setFollowup = (sid: string, question: FollowupQuestion | null) => {
    followupMapRef.current[sid] = question;
    setFollowupMap(prev => ({ ...prev, [sid]: question }));
  };
  const setPendingQueue = (sid: string, queue: PendingMessage[]) => {
    pendingQueueRef.current[sid] = queue;
    setPendingQueueMap((prev) => ({ ...prev, [sid]: queue }));
  };
  const setTodos = (sid: string, todos: TodoItem[]) =>
    setTodoMap((prev) => ({ ...prev, [sid]: todos }));

  const ensureAssistantMessage = (
    sid: string,
    patchData: Partial<UiMessage>,
  ): string => {
    const book = bookFor(sid);
    const turnStartedAt = startLocalTurn(sid);
    if (!book.assistantId) {
      book.assistantId = appendRef.current(sid, "assistant", "", {
        turnStartedAt,
        ...patchData,
      });
      return book.assistantId;
    }
    patchRef.current(sid, book.assistantId, {
      turnStartedAt,
      ...patchData,
    });
    return book.assistantId;
  };

  const showPlanSnapshot = (sid: string, review: PlanReview, active: boolean, forTurn = false) => {
    currentReviews.current.set(sid, { review, active });
    setPlan(sid, { active, review: null });
    if (forTurn) {
      ensureAssistantMessage(sid, { planReview: review });
      return;
    }
    setMessagesMap((prev) => {
      const list = [...(prev[sid] ?? [])];
      let idx = list.length - 1;
      while (idx >= 0 && list[idx].planReview?.planFile !== review.planFile) idx--;
      if (idx >= 0) list[idx] = { ...list[idx], planReview: review };
      else list.push({ id: newId(), role: "assistant", text: "", planReview: review });
      return { ...prev, [sid]: list };
    });
  };

  const finishLocalTurn = (sid: string, status: SessionStatus = "idle") => {
    const book = bookFor(sid);
    snapshotBooks.current.delete(book);
    activeRequests.current.delete(sid);
    book.toolMap.clear();
    book.assistantId = null;
    book.turnStartedAt = null;
    book.awaitingAssistantAfterTool = false;
    resetDeltaBook(book);
    book.hadTeamInternal = false;
    setQueue(sid, "");
    setBusy(sid, false);
    setStatus(sid, status);
  };

  const syncSubscriptions = (sessions: string[]) => {
    const ids = Array.from(new Set(sessions.map((id) => id.trim()).filter(Boolean)));
    const { lastSequences, cursors } = gatewaySequences.current.subscription(ids);
    sockRef.current?.subscribe(ids, lastSequences, cursors);
  };

  const setRecoveryNotice = (sid: string, notice = "") => {
    setRecoveryNotices((prev) => {
      if (prev[sid] === notice || (!notice && !(sid in prev))) return prev;
      const next = { ...prev };
      if (notice) next[sid] = notice;
      else delete next[sid];
      return next;
    });
  };

  function recoveryIsCurrent(sid: string, recovery: SessionRecovery, revision: number): boolean {
    return transportOpen.current && recoveries.current.get(sid) === recovery && recovery.revision === revision;
  }

  function scheduleRecovery(sid: string, recovery: SessionRecovery, immediate = false): void {
    if (!transportOpen.current || recoveries.current.get(sid) !== recovery) return;
    if (recovery.inFlight) {
      if (immediate) recovery.retryRequested = true;
      return;
    }
    clearTimeout(recovery.timer);
    if (immediate) {
      void checkRecovery(sid, recovery);
    } else {
      recovery.timer = setTimeout(() => {
        recovery.timer = undefined;
        void checkRecovery(sid, recovery);
      }, RECOVERY_POLL_MS);
    }
  }

  async function checkRecovery(sid: string, recovery: SessionRecovery): Promise<void> {
    const revision = recovery.revision;
    if (!recoveryIsCurrent(sid, recovery, revision)) return;
    recovery.inFlight = true;
    recovery.retryRequested = false;
    try {
      const status = await withTimeout(api.sessionStatus(sid), HISTORY_LOAD_TIMEOUT_MS, "session status");
      if (!recoveryIsCurrent(sid, recovery, revision)) return;
      if (status.live === "idle") {
        await loadHistory(sid, { recovery, revision, status });
      } else if (status.live === "running" || status.live === "queued") {
        setStatus(sid, status.live);
        setRecoveryNotice(sid, RECOVERY_RUNNING_NOTICE);
      }
    } catch {
      // Keep what the user has already seen until the Gateway can confirm a terminal state.
    } finally {
      recovery.inFlight = false;
      scheduleRecovery(sid, recovery, recovery.retryRequested);
    }
  }

  // 用 ref 固定消息操作，避免它们进入 useEffect 依赖导致 WS 重连
  const appendRef = useRef(
    (sid: string, role: MsgRole, text: string, extra?: Partial<UiMessage>): string => {
      const id = extra?.id ?? newId();
      loadedSessionsRef.current.add(sid);
      setMessagesMap((prev) => {
        const messages = prev[sid] ?? [];
        if (extra?.id && messages.some(message => message.id === id)) return prev;
        return { ...prev, [sid]: [...messages, { id, role, text, ...extra }] };
      });
      return id;
    },
  );
  const patchRef = useRef(
    (sid: string, id: string, patch: Partial<UiMessage> | ((m: UiMessage) => Partial<UiMessage>)) => {
      setMessagesMap((prev) => {
        const list = prev[sid];
        if (!list) return prev;
        return {
          ...prev,
          [sid]: list.map((m) => {
            if (m.id !== id) return m;
            const resolved = typeof patch === "function" ? patch(m) : patch;
            return { ...m, ...resolved };
          }),
        };
      });
    },
  );
  const removeRef = useRef(
    (sid: string, id: string) => {
      setMessagesMap((prev) => {
        const list = prev[sid];
        if (!list) return prev;
        return {
          ...prev,
          [sid]: list.filter((m) => m.id !== id),
        };
      });
    },
  );

  useEffect(() => {
    const append = appendRef.current;
    const patch = patchRef.current;
    const remove = removeRef.current;

    const subscribeOpenSessions = () => {
      const sessions = new Set<string>(subscribedSessionsRef.current);
      sessions.add(currentSidRef.current);
      for (const [sid, busy] of Object.entries(busyRef.current)) {
        if (busy) sessions.add(sid);
      }
      for (const [sid, queue] of Object.entries(pendingQueueRef.current)) {
        if (queue.length > 0) sessions.add(sid);
      }
      syncSubscriptions(Array.from(sessions));
      for (const [sid, recovery] of recoveries.current) scheduleRecovery(sid, recovery, true);
    };

    const applyFollowup = (c: Chunk, sid: string): boolean => {
      const status = typeof c.body.status === "string" ? c.body.status : "";
      if (closedFollowupStatuses.includes(status)) {
        const current = followupMapRef.current[sid];
        const questionId = typeof c.body.question_id === "string" ? c.body.question_id : "";
        const sameQuestion = current?.question_id === questionId;
        if (sameQuestion && current?.request_id && c.request_id && c.request_id !== current.request_id) return true;
        if (questionId && !(sameQuestion && current?.record_history === false)) {
          if (sameQuestion && status === "resolved" && c.request_id
            && c.request_id === current?.request_id && c.request_id === activeRequests.current.get(sid)
            && c.body.answer_history_id === `followup:${questionId}`
            && typeof c.body.answer_text === "string" && c.body.answer_text.trim()) {
            const book = bookFor(sid);
            startAssistantSegment(book);
            book.hadTeamInternal = false;
          }
          setMessagesMap(prev => {
            const rows = prev[sid] ?? [];
            const next = settleFollowupAnswer(rows, c.body);
            return next === rows ? prev : { ...prev, [sid]: next };
          });
        }
        if (!questionId || sameQuestion) setFollowup(sid, null);
        return true;
      }
      setFollowup(sid, { ...parseFollowupQuestion(c.body, status), request_id: c.request_id });
      return false;
    };

    const applyPlanReview = (c: Chunk, sid: string): void => {
      const review = reviewFromSnapshot(c.body);
      const active = c.body.active ?? !["readonly", "approved", "rejected", "cancelled"].includes(review.status ?? "");
      const shown = sockRef.current?.plans.overlay(sid, review) ?? review;
      // Decisions broadcast current state independently of the turn which generated the original card.
      showPlanSnapshot(sid, shown, active, Boolean(c.request_id) || !currentReviews.current.has(sid));
    };

    const beginRecovery = (c: Chunk): void => {
      const sid = c.session_id;
      const epoch = c.gateway_epoch;
      const sequence = c.gateway_sequence;
      if (!sid || !subscribedSessionsRef.current.has(sid) || typeof epoch !== "string" || !epoch
        || typeof sequence !== "number" || !Number.isSafeInteger(sequence) || sequence < 0
        || !RESYNC_REASONS.has(c.body?.reason)) return;
      const sameEpoch = gatewaySequences.current.epoch(sid) === epoch;
      if (!gatewaySequences.current.reset(sid, { epoch, sequence })) return;

      const previous = recoveries.current.get(sid);
      clearTimeout(previous?.timer);
      historyLoads.current.delete(sid);
      const previousBook = bookRef.current.get(sid);
      const snapshot = parseActiveTurnSnapshot(c.body.turn_snapshot, c);
      bookRef.current.delete(sid);
      clearTimeout(compactionTimersRef.current[sid]);
      delete compactionTimersRef.current[sid];
      delete compactionStartedAtRef.current[sid];
      setCompactionMap((prev) => prev[sid] ? { ...prev, [sid]: false } : prev);
      if (!sameEpoch) setFollowup(sid, null);
      const recovery: SessionRecovery = {
        revision: 0, inFlight: false, retryRequested: false,
        verifiedFollowupId: sameEpoch ? previous?.verifiedFollowupId : undefined,
      };
      if (Array.isArray(c.body.pending_followups)) {
        for (const question of c.body.pending_followups) {
          if (question?.kind !== "followup_question" || !question.body?.question_id
            || question.session_id !== sid) continue;
          if (!applyFollowup(question, sid)) recovery.verifiedFollowupId = question.body.question_id;
        }
      }
      if (snapshot && !suppressChunksRef.current.has(sid)
        && !isSuppressedRequest(suppressedRequestKey(sid, snapshot.request_id), suppressedRequestIdsRef.current)) {
        const projected = projectActiveTurn(snapshot, previousBook?.prevTurnFileSignature);
        const history = mapHistoryItems(snapshot.history.items);
        recoveries.current.delete(sid);
        bookRef.current.set(sid, projected.book);
        activeRequests.current.set(sid, snapshot.request_id);
        snapshotBooks.current.add(projected.book);
        loadedSessionsRef.current.add(sid);
        setMessagesMap((prev) => ({
          ...prev,
          [sid]: [
            ...mergeActiveSnapshotHistory(history, prev[sid] ?? [], snapshot.history.has_more),
            ...projected.messages,
          ],
        }));
        if (projected.todos) setTodos(sid, projected.todos);
        if (projected.compaction) {
          compactionStartedAtRef.current[sid] = Date.now();
          setCompactionMap((prev) => ({ ...prev, [sid]: true }));
        }
        setBusy(sid, true);
        setStatus(sid, projected.queued ? "queued" : "running");
        setQueue(sid, projected.queueHint);
        setRecoveryNotice(sid);
        return;
      }
      recoveries.current.set(sid, recovery);
      setBusy(sid, true);
      setQueue(sid, "");
      setRecoveryNotice(sid, RECOVERY_NOTICE);
      scheduleRecovery(sid, recovery, true);
    };

    const handle = (c: Chunk) => {
      if (c.kind === "gateway_resync") {
        beginRecovery(c);
        return;
      }
      const sid = c.session_id || currentSidRef.current;
      if (!gatewaySequences.current.accept(sid, c)) return;
      if (suppressChunksRef.current.has(sid)) return;
      const chunkRequestId = String(
        c.request_id || c.body?.request_id || (c.body?.message && typeof c.body.message === "object"
          ? c.body.message.request_id
          : "") || "",
      ).trim();
      const ack = c.kind === "status" && c.body.stop;
      if (ack && typeof ack.request_id === "string") {
        const notice = stopNotices.current.get(suppressedRequestKey(sid, ack.request_id));
        if (notice && !notice.terminal && !notice.uncertainTool) {
          if (ack.state === "requested") patch(sid, notice.id, { text: "网关已收到停止请求，正在等待回合结束。" });
          if (ack.state === "not_active") {
            patch(sid, notice.id, { text: "该回合已不在运行。" });
            if (activeRequests.current.get(sid) === ack.request_id) {
              void loadHistory(sid).then(() => {
                if (!activeRequests.current.has(sid) && !busyRef.current[sid]) consumePendingRef.current(sid);
              });
            }
          }
        }
        return;
      }
      const notice = stopNotices.current.get(suppressedRequestKey(sid, chunkRequestId));
      const unconfirmed = c.kind === "tool" && toolExitUnconfirmed(c.body.result_metadata);
      if (notice && unconfirmed) {
        notice.uncertainTool = true;
        patch(sid, notice.id, { text: "已请求停止，工具实际退出尚未确认。" });
      }
      const terminal = c.kind === "final" || c.kind === "error";
      if (notice && terminal) {
        notice.terminal = true;
        if (!notice.uncertainTool) {
          patch(sid, notice.id, { text: c.body.outcome?.status === "stopped" ? "回合已停止。" : "回合已结束。" });
        }
      }
      const identity = activeRequests.current.get(sid);
      if (chunkRequestId && (terminal || ["delta", "thinking", "tool"].includes(c.kind))) {
        if (identity && identity !== chunkRequestId) return;
        if (!identity && notice) return;
        if (!identity && !terminal) activeRequests.current.set(sid, chunkRequestId);
      }
      if (!terminal && !unconfirmed && isSuppressedRequest(suppressedRequestKey(sid, chunkRequestId), suppressedRequestIdsRef.current)) return;
      const executingPlan = sockRef.current?.plans.observeExecution(sid, chunkRequestId, terminal);
      if (executingPlan && !terminal && !busyRef.current[sid]) {
        bookRef.current.delete(sid);
        setBusy(sid, true);
      }
      const recovery = recoveries.current.get(sid);
      if (recovery) {
        // There is no complete active-turn projection. Keep the visible prefix and
        // never join it with a suffix whose beginning may have fallen out of the log.
        if (c.kind === "followup_question") {
          if (!applyFollowup(c, sid)) recovery.verifiedFollowupId = c.body.question_id;
        } else if (c.kind === "plan_review") {
          applyPlanReview(c, sid);
        }
        recovery.revision += 1;
        if (c.kind === "final" || c.kind === "error" || c.kind === "plan_review") {
          scheduleRecovery(sid, recovery, true);
        }
        return;
      }
      const book = bookFor(sid);

      const buildFeatureEventCtx = (): FeatureEventContext => ({
        sessionId: sid,
        now: Date.now(),
        startLocalTurn: () => startLocalTurn(sid),
        newId,
        book,
        messages: messagesMap[sid] ?? [],
      });

      const applyFeatureEventEffect = (effect: FeatureEventEffect | null) => {
        if (!effect) return;
        if (effect.hadTeamInternal) book.hadTeamInternal = true;
        if (effect.statusHint) setStatus(sid, effect.statusHint);
        if (effect.queueHint !== undefined) setQueue(sid, effect.queueHint);
        if (effect.messages) {
          loadedSessionsRef.current.add(sid);
          setMessagesMap((prev) => ({ ...prev, [sid]: effect.messages!(prev[sid] ?? []) }));
        }
        if (effect.wikiProgress) {
          flushSync(() => {
            setWikiProgressMap((prev) => {
              const sourceId = effect.wikiProgress!.source_id || sid;
              const next = { ...prev, [sourceId]: effect.wikiProgress! };
              wikiProgressRef.current = next;
              return next;
            });
          });
        }
        if (effect.wikiChanged) {
          window.dispatchEvent(
            new CustomEvent("crew:wiki-changed", { detail: effect.wikiChanged }),
          );
        }
      };

      if (c.kind === "feature_event") {
        const parsed = parseFeatureEventBody(c.body);
        if (parsed) {
          applyFeatureEventEffect(
            featureEventRegistryRef.current.dispatch(
              parsed.feature,
              parsed.event,
              parsed.version,
              parsed.payload,
              buildFeatureEventCtx(),
            ),
          );
        }
        return;
      }

      if (applyTurnOutput(c, book, {
        append: (role, text, extra) => append(sid, role, text, extra),
        patch: (id, update) => patch(sid, id, update),
        setTodos: (todos) => setTodos(sid, todos),
      }, Date.now())) {
        if (showsTurnOutput(c)) {
          setQueue(sid, "");
          setStatus(sid, "running");
        }
        return;
      }

      if (c.kind === "task") {
        fireAfterFinalThrottled();
      } else if (c.kind === "status") {
        const msg = c.body.message ?? "";
        if (c.body.activity === "context_compaction") {
          const timer = compactionTimersRef.current[sid];
          if (timer) {
            clearTimeout(timer);
            delete compactionTimersRef.current[sid];
          }
          if (c.body.active === true) {
            compactionStartedAtRef.current[sid] = Date.now();
            setCompactionMap((prev) => ({ ...prev, [sid]: true }));
          } else {
            const elapsed = Date.now() - (compactionStartedAtRef.current[sid] ?? 0);
            const hide = () => {
              setCompactionMap((prev) => ({ ...prev, [sid]: false }));
              delete compactionStartedAtRef.current[sid];
              delete compactionTimersRef.current[sid];
            };
            const remaining = Math.max(0, 800 - elapsed);
            if (remaining > 0) compactionTimersRef.current[sid] = setTimeout(hide, remaining);
            else hide();
          }
          return;
        }
        // 队列状态 → 小卡片；其它状态（如 Team 派发进度）仍走消息气泡
        if (msg.includes("排队")) {
          setQueue(sid, msg);
          setStatus(sid, "queued");
        } else if (isPlanControlStatus(msg)) {
          setQueue(sid, "");
          setStatus(sid, "idle");
          if (msg.startsWith("已进入 Plan 模式")) {
            setPlan(sid, { active: true });
          } else if (msg.startsWith("已退出 Plan 模式")) {
            setPlan(sid, { active: false, review: null });
          } else if (msg.startsWith("已保留 Plan 模式")) {
            updatePlanMap((prev) => {
              const cur = prev[sid] ?? { active: true, review: null };
              return {
                ...prev,
                [sid]: {
                  active: true,
                  review: cur.review ? { ...cur.review, status: "editing" } : null,
                },
              };
            });
          }
        } else if (isTeamRuntimeStatus(msg)) {
          setQueue(sid, msg);
          setStatus(sid, "running");
        }
      } else if (c.kind === "final") {
        const text = c.body.text ?? "";
        const timing = localTurnTiming(sid);
        // final 表示整个回合已结束；无论 Builtin 还是 ACP，前端都不能保留 running 工具。
        // 正常路径仍由 result/error 事件先更新，这里只处理缺失尾事件的统一兜底。
        for (const tool of book.toolMap.values()) {
          if (tool.status === "running") {
            tool.status = "done";
            tool.duration = Date.now() - tool.startedAt;
          }
        }
        if (text && book.awaitingAssistantAfterTool) {
          book.assistantId = null;
          book.toolMap.clear();
          book.awaitingAssistantAfterTool = false;
          resetDeltaBook(book);
        }
        let assistantId = book.assistantId;
        if (book.hadTeamInternal) {
          if (assistantId) {
            remove(sid, assistantId);
            assistantId = null;
            book.assistantId = null;
          }
        } else if (assistantId) {
          // final 帧只带最后一轮模型输出；delta 累积的可能是多轮全文。
          // 保留累积全文，只在 final 明显更完整（单轮 delta 丢帧兜底）时才覆盖。
          patch(sid, assistantId, (m) => ({
            text: resolveFinalText(m.text ?? "", text),
            ...timing,
          }));
        } else if (!book.hadTeamInternal) {
          assistantId = append(sid, "assistant", text, timing);
        }
        if (assistantId && book.toolMap.size > 0) {
          patch(sid, assistantId, {
            ...timing,
            toolCalls: Array.from(book.toolMap.values()),
          });
        }
        const turnFileChanges = currentTurnFileChanges(book);
        if (assistantId && turnFileChanges.length > 0) {
          patch(sid, assistantId, { turnFileChanges });
        }
        book.prevTurnFileSignature = { ...book.fileChangeSignatures };
        // 轮次结束才重置聚合（忙时连发时第一轮的尾部 delta 不会误起新消息）
        finishLocalTurn(sid, "idle");
        if (executingPlan) void loadHistory(sid);
        afterFinalRef.current();
        // 自动消费前端本地队列
        consumePendingRef.current(sid);
      } else if (c.kind === "error") {
        append(sid, "error", c.body.message ?? "未知错误", localTurnTiming(sid));
        finishLocalTurn(sid, "error");
        if (executingPlan) void loadHistory(sid);
        // 自动消费前端本地队列（stop 导致的 error 会在 stop 回调中清空队列）
        consumePendingRef.current(sid);
      } else if (c.kind === "plan_review") {
        applyPlanReview(c, sid);
      } else if (c.kind === "followup_question") {
        // 模型发起追问 → 展示选择框
        if (applyFollowup(c, sid) && typeof c.body.note === "string" && c.body.note.trim()) {
          append(sid, "assistant", c.body.note.trim(), localTurnTiming(sid));
        }
      }
    };

    const sock = new ChatSocket(handle, (open) => {
      transportOpen.current = open;
      setConnected(open);
      if (!open) {
        for (const recovery of recoveries.current.values()) {
          recovery.revision += 1;
          clearTimeout(recovery.timer);
        }
      }
    }, subscribeOpenSessions);
    sockRef.current = sock;
    sock.connect();
    return () => {
      transportOpen.current = false;
      for (const recovery of recoveries.current.values()) clearTimeout(recovery.timer);
      recoveries.current.clear();
      historyLoads.current.clear();
      sock.dispose();
    };
  }, []); // 空依赖，只在挂载时创建一次 WS

  const send = useCallback(
    (
      query: string,
      sessionId: string,
      mode: Mode,
      workspaceId: string,
      sendAttachments: Attachment[] = [],
      options: SendOptions = {},
    ) => {
      const wasBusy = Boolean(busyRef.current[sessionId] || recoveries.current.has(sessionId));
      const planActive = Boolean(planMapRef.current[sessionId]?.active);
      if (wasBusy) {
        // 该会话已有一轮在跑 → 本条进入前端本地队列
        const id = newId();
        setPendingQueue(sessionId, [
          ...(pendingQueueRef.current[sessionId] ?? []),
          {
            id,
            query,
            attachments: sendAttachments,
            mode,
            workspaceId,
            planActive,
            subScenario: options.subScenario,
            externalTeamId: options.externalTeamId,
            wikiKbId: options.wikiKbId,
            externalWikiReadKbId: options.externalWikiReadKbId,
            teamExecutionTier: options.teamExecutionTier,
            userMentions: options.userMentions,
          },
        ]);
        setQueue(sessionId, "正在排队…");
        setStatus(sessionId, "queued");
        return;
      }
      const requestId = newRequestId();
      const payload: Record<string, unknown> = {
        request_id: requestId, query,
        session_id: sessionId,
        mode,
        workspace_id: workspaceId,
        attachments: sendAttachments,
      };
      if (planActive) {
        payload.plan_active = true;
      }
      if (options.subScenario) {
        payload.sub_scenario = options.subScenario;
      }
      if (options.externalTeamId) {
        payload.external_team_id = options.externalTeamId;
      }
      if (options.wikiKbId) {
        payload.wiki_kb_id = options.wikiKbId;
      }
      if (mode === "team" && options.externalWikiReadKbId) {
        payload.external_wiki_read_kb_id = options.externalWikiReadKbId;
      }
      if (options.userMentions?.length) {
        payload.user_mentions = options.userMentions;
      }
      if (mode === "team" && options.teamExecutionTier) {
        payload.team_execution_profile = {
          requested_mode: options.teamExecutionTier,
          tier: options.teamExecutionTier,
        };
      }
      const ok = sockRef.current?.send(payload);
      if (!ok) {
        appendRef.current(sessionId, "error", "未连接到后端网关（请确认 python -m crew.gateway.server 已启动）");
        return;
      }
      activeRequests.current.set(sessionId, requestId);
      subscribedSessionsRef.current.add(sessionId);
      suppressChunksRef.current.delete(sessionId);
      startLocalTurn(sessionId);
      appendRef.current(sessionId, "user", query, {
        requestId, attachments: sendAttachments.length > 0 ? sendAttachments : undefined,
      });
      setBusy(sessionId, true);
      setStatus(sessionId, "running");
    },
    [],
  );

  /** 消费前端本地队列中的下一条消息 */
  const consumePending = useCallback((sid: string) => {
    const queue = pendingQueueRef.current[sid] ?? [];
    if (queue.length === 0) return;
    const [next, ...rest] = queue;
    setPendingQueue(sid, rest);
    const requestId = newRequestId();
    const payload: Record<string, unknown> = {
      request_id: requestId, query: next.query,
      session_id: sid,
      mode: next.mode,
      workspace_id: next.workspaceId,
      attachments: next.attachments,
    };
    if (next.planActive) {
      payload.plan_active = true;
    }
    if (next.subScenario) {
      payload.sub_scenario = next.subScenario;
    }
    if (next.externalTeamId) {
      payload.external_team_id = next.externalTeamId;
    }
    if (next.wikiKbId) {
      payload.wiki_kb_id = next.wikiKbId;
    }
    if (next.mode === "team" && next.externalWikiReadKbId) {
      payload.external_wiki_read_kb_id = next.externalWikiReadKbId;
    }
    if (next.userMentions?.length) {
      payload.user_mentions = next.userMentions;
    }
    if (next.mode === "team" && next.teamExecutionTier) {
      payload.team_execution_profile = {
        requested_mode: next.teamExecutionTier,
        tier: next.teamExecutionTier,
      };
    }
    const ok = sockRef.current?.send(payload);
    if (!ok) {
      appendRef.current(sid, "error", "未连接到后端网关（请确认 python -m crew.gateway.server 已启动）");
      setBusy(sid, false);
      setStatus(sid, "idle");
      return;
    }
    activeRequests.current.set(sid, requestId);
    subscribedSessionsRef.current.add(sid);
    suppressChunksRef.current.delete(sid);
    startLocalTurn(sid);
    appendRef.current(sid, "user", next.query, {
      requestId, attachments: next.attachments.length > 0 ? next.attachments : undefined,
    });
    setBusy(sid, true);
    setStatus(sid, "running");
  }, []);

  const consumePendingRef = useRef(consumePending);
  consumePendingRef.current = consumePending;

  const stop = useCallback((sid: string, expectedRequest?: string) => {
    const current = activeRequests.current.get(sid);
    if (current !== expectedRequest) return;
    const key = suppressedRequestKey(sid, current ?? "");
    const sent = current && sockRef.current?.stop(sid, current);
    const text = sent ? "已发送停止请求，正在等待网关确认。" : "尚未确认停止。当前连接无法停止指定回合。";
    const old = stopNotices.current.get(key);
    if (old) {
      if (!old.uncertainTool && !old.terminal) patchRef.current(sid, old.id, { text });
      return;
    }
    const id = appendRef.current(sid, "assistant", text, { requestId: current });
    stopNotices.current.set(key, { id, uncertainTool: false, terminal: false });
    if (stopNotices.current.size > 200) stopNotices.current.delete(stopNotices.current.keys().next().value!);
  }, []);

  const cancelMention = useCallback((sessionId: string, requestId?: string) => {
    const normalizedRequestId = String(requestId || "").trim();
    if (!normalizedRequestId || normalizedRequestId !== activeRequests.current.get(sessionId)) return;
    stop(sessionId, normalizedRequestId);
    suppressedRequestIdsRef.current.add(suppressedRequestKey(sessionId, normalizedRequestId));
    // 只保留最近的取消请求，避免长时间打开页面后集合无限增长。
    while (suppressedRequestIdsRef.current.size > 200) {
      const oldest = suppressedRequestIdsRef.current.values().next().value;
      if (oldest == null) break;
      suppressedRequestIdsRef.current.delete(oldest);
    }

    setMessagesMap((prev) => ({
      ...prev,
      [sessionId]: (prev[sessionId] ?? []).map((message) => message.requestId === normalizedRequestId
        ? { ...message, communicationStatus: "cancelled" }
        : message),
    }));
  }, [stop]);

  /** 向运行中的回复注入补充指令（不打断；后端回 status 帧确认）。
   *  本地把这句作为 user 气泡显示，让用户看见自己注入了什么。 */
  const steer = useCallback((sessionId: string, text: string) => {
    const t = text.trim();
    if (!t) return;
    const ok = sockRef.current?.steer(sessionId, t);
    if (ok) appendRef.current(sessionId, "user", t);
  }, []);

  /** 进入 Plan 模式（只读探索→写计划→审批后执行）。 */
  const enterPlan = useCallback((sessionId: string) => {
    setPlan(sessionId, { active: true });
  }, []);

  /** 退出 Plan 模式，切回普通 Craft。 */
  const exitPlan = useCallback((sessionId: string) => {
    const sessionExists = subscribedSessionsRef.current.has(sessionId) || loadedSessionsRef.current.has(sessionId);
    if (!sessionExists || sockRef.current?.planExit(sessionId)) {
      setPlan(sessionId, { active: false, review: null });
    }
  }, []);

  const decidePlan = useCallback((action: PlanDecision, review: PlanReview, sid: string, mode: Mode, workspaceId: string) => {
    const sock = sockRef.current;
    if (!sock) return;
    sock.plans.submit(action, review, sid, mode, workspaceId, ({ notice, snapshot }) => {
      const current = currentReviews.current.get(sid)?.review;
      const authoritative = snapshot && reviewFromSnapshot(snapshot);
      if (snapshot && authoritative && (!current || samePlan(current, review) || samePlan(current, authoritative))) {
        showPlanSnapshot(sid, { ...authoritative, decisionNotice: notice }, snapshot.active);
      } else {
        setMessagesMap((prev) => ({
          ...prev, [sid]: (prev[sid] ?? []).map((m) => m.planReview && samePlan(m.planReview, review)
            ? { ...m, planReview: { ...m.planReview, ...(notice === "expired" ? { status: "readonly" as const } : {}), decisionNotice: notice } } : m),
        }));
      }
    });
  }, []);

  /** 提交追问答案。 */
  const answerFollowup = useCallback(
    (sessionId: string, questionId: string, answers: { question_id: string; answers: string[] }[]) => {
      const question = followupMapRef.current[sessionId];
      if (question?.question_id !== questionId || !sockRef.current?.followupAnswer(sessionId, questionId, answers)) return false;
      const waitsForPermissionAck = question?.record_history === false
        && ["acp_permission", "team_control"].includes(question.origin?.type ?? "");
      if (!waitsForPermissionAck) {
        setFollowup(sessionId, null);
        if (question?.record_history !== false) {
          // Ordinary follow-ups become a new user turn.
          const book = bookFor(sessionId);
          startAssistantSegment(book);
          book.hadTeamInternal = false;
          appendRef.current(sessionId, "user", `已选择：${formatFollowupAnswers(question, answers)}`, {
            id: `followup:${questionId}`, pendingFollowupAnswer: true,
          });
        }
      }
      // External ACP permission prompts stay visible until the gateway
      // confirms that the exact question id was resolved.
      return true;
    },
    [],
  );

  /** 关闭追问选择框：通知后端取消等待，并清空前端状态。 */
  const dismissFollowup = useCallback((sessionId: string) => {
    const q = followupMapRef.current[sessionId];
    if (q?.question_id) sockRef.current?.followupCancel(sessionId, q.question_id);
    setFollowup(sessionId, null);
  }, []);

  const removeFromQueue = useCallback((sessionId: string, index: number) => {
    setPendingQueueMap((prev) => {
      const list = prev[sessionId] ?? [];
      if (index < 0 || index >= list.length) return prev;
      const next = { ...prev, [sessionId]: list.filter((_, i) => i !== index) };
      pendingQueueRef.current = next;
      return next;
    });
  }, []);

  /** 立即发送队列中的某一条：先从本地队列移除，再调用 send。
   *  按钮在 busy 时禁用，因此正常情况下当前会话空闲，send 会直接发出；
   *  若意外在 busy 时被调用，send 会把它重新加入队列末尾兜底。 */
  const sendQueueItemNow = useCallback((sessionId: string, id: string) => {
    const list = pendingQueueRef.current[sessionId] ?? [];
    const index = list.findIndex((item) => item.id === id);
    if (index < 0) return;
    const item = list[index];
    const next = [...list.slice(0, index), ...list.slice(index + 1)];
    setPendingQueue(sessionId, next);
    send(item.query, sessionId, item.mode, item.workspaceId, item.attachments, {
      subScenario: item.subScenario,
      externalTeamId: item.externalTeamId,
      wikiKbId: item.wikiKbId,
      externalWikiReadKbId: item.externalWikiReadKbId,
      teamExecutionTier: item.teamExecutionTier,
      userMentions: item.userMentions,
    });
  }, [send]);

  const editQueueItem = useCallback((sessionId: string, index: number, newQuery: string) => {
    setPendingQueueMap((prev) => {
      const list = prev[sessionId] ?? [];
      if (index < 0 || index >= list.length) return prev;
      const nextList = list.map((item, i) => (i === index ? { ...item, query: newQuery } : item));
      const next = { ...prev, [sessionId]: nextList };
      pendingQueueRef.current = next;
      return next;
    });
  }, []);

  const loadHistory = useCallback(async (sessionId: string, resume?: RecoveryHistoryLoad) => {
    const book = bookRef.current.get(sessionId);
    if (!resume && (recoveries.current.has(sessionId) || (book && snapshotBooks.current.has(book)))) return;
    const load = Symbol();
    historyLoads.current.set(sessionId, load);
    const requestAtLoad = activeRequests.current.get(sessionId);
    const isCurrent = () => activeRequests.current.get(sessionId) === requestAtLoad && historyLoads.current.get(sessionId) === load
      && (!resume || recoveryIsCurrent(sessionId, resume.recovery, resume.revision));
    const isLoaded = loadedSessionsRef.current.has(sessionId);
    const wasRunning = busyRef.current[sessionId] || !!bookRef.current.get(sessionId)?.assistantId;

    // 对 running/不完整的会话重新同步时，先清聚合记账，避免旧 toolMap/assistantId
    // 与新 chunk 或后端历史冲突。
    if (!resume && isLoaded && wasRunning) {
      bookFor(sessionId).toolMap.clear();
      bookFor(sessionId).assistantId = null;
      bookFor(sessionId).awaitingAssistantAfterTool = false;
      resetDeltaBook(bookFor(sessionId));
      bookFor(sessionId).hadTeamInternal = false;
    }

    subscribedSessionsRef.current.add(sessionId);
    if (!resume) setQueue(sessionId, "");
    if (!resume && !isLoaded && !wasRunning) {
      setMessagesMap((prev) => ({
        ...prev,
        [sessionId]: [{ id: newId(), role: "assistant" as MsgRole, text: HISTORY_LOADING_TEXT }],
      }));
    }
    try {
      // 并行取历史消息 + 当前运行态 + plan 状态。
      const beforePlan = currentReviews.current.get(sessionId);
      const plan = api.sessionPlan(sessionId);
      const todos = api.sessionTodos(sessionId);
      const [items, st, planState, todoState] = await Promise.all([
        withTimeout(api.history(sessionId), HISTORY_LOAD_TIMEOUT_MS, "history"),
        resume ? Promise.resolve(resume.status) : api.sessionStatus(sessionId).catch(() => null),
        resume ? withTimeout(plan, HISTORY_LOAD_TIMEOUT_MS, "plan") : plan.catch(() => null),
        resume ? withTimeout(todos, HISTORY_LOAD_TIMEOUT_MS, "todos") : todos.catch(() => ({ todos: [] })),
      ]);
      if (!isCurrent()) return;
      const list = mapHistoryItems(items);
      if (st && st.last_status === "failed" && st.last_error) {
        list.push({ id: newId(), role: "error" as MsgRole, text: st.last_error });
      }
      // 重建 planReview 卡片：定位 plan 轮——最后一条 toolCalls 含 exit_plan_mode 的
      // assistant 消息（后端历史把 exit_plan_mode 作为 tool_call 落在该轮）。把卡片挂到
      // plan 轮而非会话末尾，满足「在哪轮写就留在哪轮」；awaiting 时 plan 轮即最新轮。
      const latest = currentReviews.current.get(sessionId);
      const fromLive = latest && latest !== beforePlan;
      const restored = planState?.has_plan && planState.plan ? reviewFromSnapshot(planState) : undefined;
      const fetchedReview = restored && (sockRef.current?.plans.overlay(sessionId, restored) ?? restored);
      const review = fromLive ? latest.review : fetchedReview;
      const active = fromLive ? latest.active : Boolean(planState?.active);
      if (planState && !fromLive) sockRef.current?.plans.resumeExecution(sessionId, planState);
      if (review) {
        currentReviews.current.set(sessionId, { review, active });
        if (!list.some((m) => m.planReview?.planFile === review.planFile && m.planReview?.plan === review.plan)) {
          let planIdx = -1;
          for (let i = list.length - 1; i >= 0; i--) {
            const m = list[i];
            if (m.role === "assistant" && m.toolCalls?.some((tc) => tc.name === "exit_plan_mode")) {
              planIdx = i;
              break;
            }
          }
          if (planIdx >= 0) {
            list[planIdx] = { ...list[planIdx], planReview: review };
          } else {
            // 历史缺失 exit_plan_mode（极旧会话），回退到末尾，保证卡片可见。
            list.push({ id: newId(), role: "assistant" as MsgRole, text: "", planReview: review });
          }
        }
      }
      // 把最新 todo 快照挂到历史最后一条 assistant 消息，使刷新后历史回合仍渲染 todo 卡片。
      const histTodos = normalizeTodos(todoState?.todos);
      if (histTodos.length > 0) {
        let lastAssistant = -1;
        for (let i = list.length - 1; i >= 0; i--) {
          if (list[i].role === "assistant") {
            lastAssistant = i;
            break;
          }
        }
        if (lastAssistant >= 0) {
          list[lastAssistant] = { ...list[lastAssistant], todoSnapshot: histTodos };
        }
      }
      loadedSessionsRef.current.add(sessionId);

      if (st?.live === "running") {
        if (st.active_request_id) activeRequests.current.set(sessionId, st.active_request_id);
        // 仍在运行：恢复 busy/状态，后续 chunk 继续追加。
        setBusy(sessionId, true);
        setStatus(sessionId, "running");
        // 刷新页面后本地消息桶为空（React 状态丢失），只剩订阅后到达的 live 片段；
        // 首次加载用后端历史回填到 live 之前，避免之前已完成的轮次丢失。运行中那一轮
        // 尚未落库，历史与 live 不重叠，不会重复。后台切回（isLoaded）本地已含完整历史，跳过。
        if (!isLoaded) {
          setMessagesMap((prev) => {
            const live = (prev[sessionId] ?? []).filter((m) => !isHistoryLoadingMessage(m));
            return {
              ...prev,
              [sessionId]: mergeHistoryWithLiveMessages(list, live),
            };
          });
        }
      } else {
        activeRequests.current.delete(sessionId);
        // 已结束：用后端历史覆盖本地，补齐可能错过的 final 文本。
        setBusy(sessionId, false);
        setMessagesMap((prev) => ({
          ...prev,
          [sessionId]: resume ? list : preserveLocalProcessDetails(list, prev[sessionId] ?? []),
        }));
        if (st?.live === "queued") setStatus(sessionId, "queued");
        else if (st?.last_status === "failed") setStatus(sessionId, "error");
        else setStatus(sessionId, "idle");
      }

      setPlan(sessionId, { active, review: null });
      setTodos(sessionId, normalizeTodos(todoState?.todos));
      if (resume) {
        recoveries.current.delete(sessionId);
        bookRef.current.delete(sessionId);
        setRecoveryNotice(sessionId);
        if (followupMapRef.current[sessionId]?.question_id !== resume.recovery.verifiedFollowupId) {
          setFollowup(sessionId, null);
        }
        const awaitingPlan = planState?.awaiting_approval || planState?.status === "pending";
        if (!awaitingPlan && !followupMapRef.current[sessionId]) consumePendingRef.current(sessionId);
      } else {
        syncSubscriptions(Array.from(subscribedSessionsRef.current));
      }
    } catch {
      if (!isCurrent() || resume) return;
      loadedSessionsRef.current.add(sessionId);
      if (!wasRunning) {
        setBusy(sessionId, false);
        setMessagesMap((prev) => ({
          ...prev,
          [sessionId]: [{ id: newId(), role: "error" as MsgRole, text: "历史加载失败，请稍后重试" }],
        }));
        setPlan(sessionId, { active: false, review: null });
        setStatus(sessionId, "error");
      }
    } finally {
      if (historyLoads.current.get(sessionId) === load) historyLoads.current.delete(sessionId);
    }
  }, []);

  /** 用后端返回的会话状态播种状态点（刷新后恢复运行中/排队中/出错）。
   *  不覆盖本地正在跑的会话（本地实时态更准）。 */
  const seedStatuses = useCallback((seed: Record<string, string>) => {
    setStatusMap((prev) => {
      const next = { ...prev };
      for (const [sid, st] of Object.entries(seed)) {
        if (st === "running" || st === "queued") {
          subscribedSessionsRef.current.add(sid);
        }
        if (busyRef.current[sid]) continue; // 本地正在跑 → 以本地为准
        next[sid] = (st as SessionStatus) ?? "idle";
      }
      return next;
    });
    syncSubscriptions(Array.from(subscribedSessionsRef.current));
  }, []);

  /** 删除/清理某会话的本地缓存（删除会话时调用）。 */
  const clearSession = useCallback((sessionId: string) => {
    activeRequests.current.delete(sessionId);
    for (const key of stopNotices.current.keys()) {
      if (key.startsWith(suppressedRequestKey(sessionId, ""))) stopNotices.current.delete(key);
    }
    currentReviews.current.delete(sessionId);
    sockRef.current?.plans.forget(sessionId);
    const compactionTimer = compactionTimersRef.current[sessionId];
    if (compactionTimer) clearTimeout(compactionTimer);
    delete compactionTimersRef.current[sessionId];
    delete compactionStartedAtRef.current[sessionId];
    bookRef.current.delete(sessionId);
    subscribedSessionsRef.current.delete(sessionId);
    suppressChunksRef.current.delete(sessionId);
    setFollowup(sessionId, null);
    loadedSessionsRef.current.delete(sessionId);
    delete busyRef.current[sessionId];
    delete pendingQueueRef.current[sessionId];
    const drop = <T,>(prev: Record<string, T>) => {
      const next = { ...prev };
      delete next[sessionId];
      return next;
    };
    setMessagesMap(drop);
    setBusyMap(drop);
    setQueueMap(drop);
    setRecoveryNotices(drop);
    setStatusMap(drop);
    setPendingQueueMap(drop);
    updatePlanMap(drop);
    setTodoMap(drop);
    setCompactionMap(drop);
    setWikiProgressMap((prev) => {
      const next: Record<string, WikiIngestProgress> = {};
      for (const [sourceId, p] of Object.entries(prev)) {
        if (p.session_id !== sessionId) next[sourceId] = p;
      }
      wikiProgressRef.current = next;
      return next;
    });
    gatewaySequences.current.delete(sessionId);
    clearTimeout(recoveries.current.get(sessionId)?.timer);
    recoveries.current.delete(sessionId);
    historyLoads.current.delete(sessionId);
  }, []);

  const plan = planMap[currentSessionId] ?? { active: false, review: null };
  return {
    messages: messagesMap[currentSessionId] ?? [],
    busy: busyMap[currentSessionId] ?? false,
    activeRequestId: activeRequests.current.get(currentSessionId),
    queueHint: queueMap[currentSessionId] ?? "",
    recoveryNotice: recoveryNotices[currentSessionId] ?? "",
    pendingQueue: pendingQueueMap[currentSessionId] ?? [],
    todos: todoMap[currentSessionId] ?? [],
    compactingContext: compactionMap[currentSessionId] ?? false,
    sessionStatus: statusMap,
    connected,
    planActive: plan.active,
    planReview: plan.review,
    wikiProgress: wikiProgressMap,
    followupQuestion: followupMap[currentSessionId] ?? null,
    // 批量获取某 session 的数据字段（Wiki Agent 等独立会话场景使用）
    forSession: (sid: string) => ({
      messages: messagesMap[sid] ?? [],
      busy: busyMap[sid] ?? false,
      activeRequestId: activeRequests.current.get(sid),
      queueHint: queueMap[sid] ?? "",
      recoveryNotice: recoveryNotices[sid] ?? "",
      pendingQueue: pendingQueueMap[sid] ?? [],
      planActive: (planMap[sid] ?? { active: false, review: null }).active,
      followupQuestion: followupMap[sid] ?? null,
      todos: todoMap[sid] ?? [],
      compactingContext: compactionMap[sid] ?? false,
    }),
    send,
    stop,
    cancelMention,
    steer,
    enterPlan,
    exitPlan,
    decidePlan,
    answerFollowup,
    dismissFollowup,
    loadHistory,
    clearSession,
    seedStatuses,
    removeFromQueue,
    editQueueItem,
    sendQueueItemNow,
  };
}
