import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { api } from "../api";
import { mapHistoryItems, mergeHistoryWithLiveMessages, normalizeTurnFileChanges, preserveLocalProcessDetails } from "../lib/historyMap";
import { mergeTeamInternalMessage } from "../lib/teamMessageMerge";
import { mergeStreamingText } from "../lib/agentTurnState";
import { backendDurationToMs, backendSecondsToMs } from "../lib/backendTime";
import { ChatSocket } from "../ws";
import type { Attachment, Chunk, FollowupQuestion, Mode, MsgRole, PendingMessage, PlanReview, TeamExecutionTier, TodoItem, ToolCallInfo, TurnFileChangeSummary, UiMessage, UserAgentMention, WikiIngestProgress, WikiPage } from "../types";

let _seq = 0;
const newId = () => `m${Date.now()}_${_seq++}`;
const HISTORY_LOADING_TEXT = "正在加载历史记录…";
const HISTORY_LOAD_TIMEOUT_MS = 8000;

function isHistoryLoadingMessage(msg: UiMessage): boolean {
  return msg.role === "assistant" && msg.text === HISTORY_LOADING_TEXT && !msg.thinking && !msg.toolCalls?.length;
}

function normalizeTeamText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    for (const key of ["message", "text", "content", "summary"]) {
      const candidate = record[key];
      if (typeof candidate === "string" && candidate.trim()) return candidate;
    }
    try {
      return JSON.stringify(value);
    } catch {
      return "";
    }
  }
  return String(value);
}

function normalizeChunkToolCalls(raw: unknown): ToolCallInfo[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const calls = raw.map((item, index) => {
    const value = item && typeof item === "object" ? item as Record<string, unknown> : {};
    return {
      toolCallId: String(value.id || value.tool_call_id || `team_tool_${index}`),
      name: String(value.name || "unknown"),
      uiLabel: typeof value.ui_label === "string" ? value.ui_label : undefined,
      args: typeof value.arguments === "string" ? value.arguments : JSON.stringify(value.arguments || {}),
      result: typeof value.result === "string" ? value.result : "",
      status: value.status === "running" || value.status === "error" ? value.status : "done",
      startedAt: typeof value.started_at === "number" ? value.started_at * 1000 : 0,
      duration: typeof value.duration === "number" ? backendDurationToMs(value.duration) || undefined : undefined,
    } satisfies ToolCallInfo;
  });
  return calls.length > 0 ? calls : undefined;
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
export type SessionStatus = "idle" | "running" | "queued" | "error";

type PlanState = { active: boolean; review: PlanReview | null };
interface SendOptions {
  subScenario?: string;
  externalTeamId?: string;
  wikiKbId?: string;
  teamExecutionTier?: TeamExecutionTier;
  userMentions?: UserAgentMention[];
}

/** 单会话的聚合记账（不直接驱动渲染，放 ref）。 */
interface Bookkeeping {
  toolMap: Map<string, ToolCallInfo>;
  assistantId: string | null;
  turnStartedAt: number | null;
  awaitingAssistantAfterTool: boolean;
  deltaSpans: DeltaSpan[];
  legacyDeltaText: string;
  hadTeamInternal: boolean;
  fileChanges: TurnFileChangeSummary[];
  fileChangeSignatures: Record<string, string>;
  prevTurnFileSignature: Record<string, string>;
}

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

export interface DeltaSpan {
  start: number;
  end: number;
  text: string;
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
  if (existing) {
    next.set(toolCallId, {
      ...existing,
      name: b.name ?? existing.name,
      uiLabel: b.ui_label ?? existing.uiLabel,
      args: b.args ?? existing.args,
      status: b.phase === "error" ? "error" : "done",
      result: b.detail ?? existing.result ?? "",
      duration: now - existing.startedAt,
    });
  } else {
    next.set(toolCallId, {
      toolCallId,
      name: b.name ?? "unknown",
      uiLabel: b.ui_label ?? "",
      args: b.args ?? "",
      result: b.detail ?? "",
      status: b.phase === "error" ? "error" : "done",
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
export function useChat(currentSessionId: string, onAfterFinal: () => void) {
  const [messagesMap, setMessagesMap] = useState<Record<string, UiMessage[]>>({});
  const [busyMap, setBusyMap] = useState<Record<string, boolean>>({});
  const [queueMap, setQueueMap] = useState<Record<string, string>>({});
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
  const planMapRef = useRef<Record<string, PlanState>>({});
  const followupMapRef = useRef<Record<string, FollowupQuestion | null>>({});
  // wikiProgressMap 的 ref 镜像，key 同样是 source_id。
  const wikiProgressRef = useRef<Record<string, WikiIngestProgress>>({});
  const subscribedSessionsRef = useRef<Set<string>>(new Set());
  const suppressChunksRef = useRef<Set<string>>(new Set());
  const suppressedRequestIdsRef = useRef<Set<string>>(new Set());
  const loadedSessionsRef = useRef<Set<string>>(new Set());
  // 各会话最后收到的 Gateway sequence（断线重连后回放定位用）
  const lastGatewaySequenceRef = useRef<Map<string, number>>(new Map());
  // Gateway 帧可能乱序到达：用 seen set 去重，不能按“更小即丢弃”的单调规则过滤。
  const seenGatewaySequencesRef = useRef<Map<string, Set<number>>>(new Map());

  const bookFor = (sid: string): Bookkeeping => {
    let b = bookRef.current.get(sid);
    if (!b) {
      b = {
        toolMap: new Map(),
        assistantId: null,
        turnStartedAt: null,
        awaitingAssistantAfterTool: false,
        deltaSpans: [],
        legacyDeltaText: "",
        hadTeamInternal: false,
        fileChanges: [],
        fileChangeSignatures: {},
        prevTurnFileSignature: {},
      };
      bookRef.current.set(sid, b);
    }
    return b;
  };

  const startLocalTurn = (sid: string): number => {
    const book = bookFor(sid);
    if (book.turnStartedAt == null) book.turnStartedAt = Date.now();
    return book.turnStartedAt;
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

  const setFollowup = (sid: string, question: FollowupQuestion | null) =>
    setFollowupMap((prev) => {
      followupMapRef.current[sid] = question;
      return { ...prev, [sid]: question };
    });
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

  const patchPlanReviewMessages = (
    sid: string,
    status: NonNullable<PlanReview["status"]>,
  ) => {
    setMessagesMap((prev) => {
      const list = prev[sid];
      if (!list) return prev;
      return {
        ...prev,
        [sid]: list.map((m) =>
          m.planReview ? { ...m, planReview: { ...m.planReview, status } } : m,
        ),
      };
    });
  };

  const finishLocalTurn = (sid: string, status: SessionStatus = "idle") => {
    const book = bookFor(sid);
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
    const lastSequences: Record<string, number> = {};
    for (const sid of sessions) {
      const seq = lastGatewaySequenceRef.current.get(sid);
      if (seq != null) lastSequences[sid] = seq;
    }
    sockRef.current?.subscribe(
      Array.from(new Set(sessions.map((id) => id.trim()).filter(Boolean))),
      lastSequences,
    );
  };

  // 用 ref 固定消息操作，避免它们进入 useEffect 依赖导致 WS 重连
  const appendRef = useRef(
    (sid: string, role: MsgRole, text: string, extra?: Partial<UiMessage>): string => {
      const id = newId();
      loadedSessionsRef.current.add(sid);
      setMessagesMap((prev) => ({ ...prev, [sid]: [...(prev[sid] ?? []), { id, role, text, ...extra }] }));
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
    };

    const handle = (c: Chunk) => {
      const sid = c.session_id || currentSidRef.current;
      if (typeof c.gateway_sequence === "number" && c.gateway_sequence > 0) {
        let seen = seenGatewaySequencesRef.current.get(sid);
        if (!seen) {
          seen = new Set<number>();
          seenGatewaySequencesRef.current.set(sid, seen);
        }
        if (seen.has(c.gateway_sequence)) return;
        seen.add(c.gateway_sequence);
        if (seen.size > 2000) {
          const keepFrom = c.gateway_sequence - 1000;
          for (const value of seen) {
            if (value < keepFrom) seen.delete(value);
          }
        }
        const previousSequence = lastGatewaySequenceRef.current.get(sid) ?? 0;
        if (c.gateway_sequence > previousSequence) {
          lastGatewaySequenceRef.current.set(sid, c.gateway_sequence);
        }
      }
      if (suppressChunksRef.current.has(sid)) return;
      const chunkRequestId = String(
        c.request_id || c.body?.request_id || (c.body?.message && typeof c.body.message === "object"
          ? c.body.message.request_id
          : "") || "",
      ).trim();
      if (isSuppressedRequest(suppressedRequestKey(sid, chunkRequestId), suppressedRequestIdsRef.current)) return;
      const book = bookFor(sid);

      if (c.kind === "delta") {
        const text = normalizeTeamText(c.body.text);
        const turnStartedAt = startLocalTurn(sid);
        setQueue(sid, "");            // 开始产出 → 清队列提示
        setStatus(sid, "running");
        if (book.awaitingAssistantAfterTool) {
          book.assistantId = null;
          book.toolMap.clear();
        }
        const nextText = applyAssistantTextDelta(book, c, text);
        if (!book.assistantId) {
          console.log(
            "[useChat delta] new assistant msg, text=",
            text.slice(0, 50).replace(/\n/g, "\\n"),
          );
          book.assistantId = append(sid, "assistant", nextText, { turnStartedAt });
        } else {
          console.log(
            "[useChat delta] append chunk, text=",
            text.slice(0, 50).replace(/\n/g, "\\n"),
          );
          patch(sid, book.assistantId, (m) => {
            console.log(
              "[useChat delta] merged len=", nextText.length,
              "endsWithNewline=", nextText.endsWith("\n"),
              "tail=", nextText.slice(-40).replace(/\n/g, "\\n"),
            );
            return { text: nextText, turnStartedAt: m.turnStartedAt ?? turnStartedAt };
          });
        }
      } else if (c.kind === "thinking") {
        const thinkingText = c.body.text ?? "";
        const hasThinkingText = Boolean(thinkingText.trim());
        setQueue(sid, "");
        setStatus(sid, "running");
        if (book.awaitingAssistantAfterTool && hasThinkingText) {
          book.assistantId = append(sid, "assistant", "", {
            turnStartedAt: startLocalTurn(sid),
            thinking: thinkingText,
          });
          book.toolMap.clear();
          book.awaitingAssistantAfterTool = false;
        } else if (!book.assistantId && hasThinkingText) {
          book.assistantId = append(sid, "assistant", "", {
            turnStartedAt: startLocalTurn(sid),
            thinking: thinkingText,
          });
        } else if (book.assistantId && hasThinkingText) {
          patch(sid, book.assistantId, (message) => ({
            thinking: mergeStreamingText(message.thinking, thinkingText),
          }));
          book.awaitingAssistantAfterTool = false;
        }
      } else if (c.kind === "tool") {
        const b = c.body;
        const turnStartedAt = startLocalTurn(sid);
        setQueue(sid, "");
        setStatus(sid, "running");
        book.toolMap = applyToolChunk(book.toolMap, b as ToolChunkBody, Date.now(), newId);
        if (!book.assistantId) {
          book.assistantId = append(sid, "assistant", "", {
            turnStartedAt,
            toolCalls: Array.from(book.toolMap.values()),
          });
        } else if (book.assistantId) {
          patch(sid, book.assistantId, {
            turnStartedAt,
            toolCalls: Array.from(book.toolMap.values()),
          });
        }
        if (b.phase !== "start") {
          book.awaitingAssistantAfterTool = true;
        }
      } else if (c.kind === "task") {
        fireAfterFinalThrottled();
      } else if (c.kind === "team_internal") {
        book.hadTeamInternal = true;
        setQueue(sid, "");
        setStatus(sid, "running");
        const sourceSessionId = c.body.source_session_id;
        const agentId = c.body.agent_id;
        const text = c.body.text ?? "";
        const eventType = c.body.event_type;
        const nodeId = c.body.node_id;
        const mentionIntent = c.body.mention_intent;
        loadedSessionsRef.current.add(sid);
        setMessagesMap((prev) => {
          const list = prev[sid] ?? [];
          const incoming: UiMessage = {
            id: newId(),
            role: "team_internal",
            text,
            sourceSessionId,
            agentId,
            agentName: c.body.agent_name,
            agentRole: c.body.agent_role,
            agentTone: c.body.agent_tone,
            isLeader: c.body.is_leader,
            eventType,
            nodeId,
            mentionFrom: c.body.mention_from,
            mentionTo: c.body.mention_to,
            mentionIntent,
            communicationKind: c.body.communication_kind,
            communicationStatus: c.body.communication_status,
            requestId: c.body.request_id,
            replyTo: c.body.reply_to,
            communicationRequestText: c.body.communication_request_text,
            displayMode: c.body.display_mode,
            collapsedTitle: c.body.collapsed_title,
            thinking: normalizeTeamText(c.body.thinking),
            toolCalls: normalizeChunkToolCalls(c.body.tool_calls),
            artifacts: c.body.artifacts,
            turnFileChanges: normalizeTurnFileChanges(c.body.turn_file_changes),
            timestamp: backendSecondsToMs(c.body.timestamp) ?? Date.now(),
            turnStartedAt: backendSecondsToMs(c.body.turn_started_at) ?? startLocalTurn(sid),
            turnDurationMs: c.body.turn_duration != null ? backendDurationToMs(c.body.turn_duration) : undefined,
          };
          return {
            ...prev,
            [sid]: mergeTeamInternalMessage(list, incoming, { append: Boolean(c.body.append) }),
          };
        });
      } else if (c.kind === "file_changes") {
        book.fileChanges = normalizeTurnFileChanges(c.body.files) ?? [];
        book.fileChangeSignatures = snapshotFileSignatures(book.fileChanges, c.body.files);
      } else if (c.kind === "todo_updated") {
        const todos = normalizeTodos(c.body.todos);
        setTodos(sid, todos);
      } else if (c.kind === "wiki_cards") {
        const pages = normalizeWikiCardPages(c.body);
        if (pages.length > 0) {
          ensureAssistantMessage(sid, { wikiCards: pages });
        }
      } else if (c.kind === "wiki_ingest_progress") {
        const progress: WikiIngestProgress = {
          stage: String(c.body.stage ?? ""),
          percent: Math.max(0, Math.min(100, Number(c.body.percent ?? 0))),
          label: String(c.body.label ?? c.body.stage ?? ""),
          source_id: String(c.body.source_id ?? ""),
          session_id: sid,
          error: typeof c.body.error === "string" ? c.body.error : undefined,
          detail: c.body.detail && typeof c.body.detail === "object" ? c.body.detail : undefined,
        };
        console.log("[wiki progress] received", sid, progress);
        // 使用 flushSync 强制同步刷新，避免 React 自动批处理把密集到达的
        // entities / topics 等快速阶段合并成一次渲染，导致中间阶段被跳过。
        // key 用 source_id 而非 session_id，保证多文件同时上传时进度互不覆盖。
        flushSync(() => {
          setWikiProgressMap((prev) => {
            const sourceId = progress.source_id || sid;
            const next = { ...prev, [sourceId]: progress };
            wikiProgressRef.current = next;
            return next;
          });
        });
      } else if (c.kind === "wiki_changed") {
        // Wiki 数据被本会话（含其委派的 Wiki 子代理）修改：广播给 WikiHub 等视图刷新，
        // 避免知识库/页面变更后必须重新进入页面才能看到。
        window.dispatchEvent(
          new CustomEvent("crew:wiki-changed", { detail: c.body?.changes ?? [] }),
        );
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
        } else {
          setQueue(sid, "");
          setStatus(sid, "running");
          append(sid, "status", msg, { turnStartedAt: startLocalTurn(sid) });
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
        afterFinalRef.current();
        // 自动消费前端本地队列
        consumePendingRef.current(sid);
      } else if (c.kind === "error") {
        append(sid, "error", c.body.message ?? "未知错误", localTurnTiming(sid));
        finishLocalTurn(sid, "error");
        // 自动消费前端本地队列（stop 导致的 error 会在 stop 回调中清空队列）
        consumePendingRef.current(sid);
      } else if (c.kind === "plan_review") {
        // 模型调了 exit_plan_mode → 写入当前回合的消息流（plan 轮），位置与工具调用卡片一致。
        // empty=true 表示计划文件为空，前端弹提示卡（无审批按钮）；否则弹正常审批卡。
        const isEmpty = !!c.body.empty;
        const review: PlanReview = {
          plan: c.body.plan ?? "",
          planFile: c.body.plan_file ?? "",
          status: isEmpty ? "empty" : (c.body.status ?? "pending"),
          empty: isEmpty,
          phase: typeof c.body.phase === "string" ? c.body.phase : undefined,
        };
        ensureAssistantMessage(sid, { planReview: review });
        setPlan(sid, {
          active: true,
          review: null,
        });
      } else if (c.kind === "followup_question") {
        // 模型发起追问 → 展示选择框
        const status = typeof c.body.status === "string" ? c.body.status : "";
        if (["expired", "cancelled", "resolved"].includes(status)) {
          const current = followupMapRef.current[sid];
          const questionId = typeof c.body.question_id === "string" ? c.body.question_id : "";
          if (!questionId || current?.question_id === questionId) {
            setFollowup(sid, null);
          }
          if (typeof c.body.note === "string" && c.body.note.trim()) {
            append(sid, "assistant", c.body.note.trim(), localTurnTiming(sid));
          }
          return;
        }
        const q: FollowupQuestion = {
          question_id: c.body.question_id ?? "",
          title: c.body.title ?? "",
          record_history: c.body.record_history !== false,
          status,
          note: typeof c.body.note === "string" ? c.body.note : undefined,
          origin: c.body.origin && typeof c.body.origin === "object" ? c.body.origin : undefined,
          questions: Array.isArray(c.body.questions)
            ? c.body.questions.map((item: any) => ({
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
        setFollowup(sid, q);
      }
    };

    const sock = new ChatSocket(handle, setConnected, subscribeOpenSessions);
    sock.connect();
    sockRef.current = sock;
    return () => sock.dispose();
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
      const wasBusy = !!busyRef.current[sessionId];
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
            teamExecutionTier: options.teamExecutionTier,
            userMentions: options.userMentions,
          },
        ]);
        setQueue(sessionId, "正在排队…");
        setStatus(sessionId, "queued");
        return;
      }
      const payload: Record<string, unknown> = {
        query,
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
      subscribedSessionsRef.current.add(sessionId);
      suppressChunksRef.current.delete(sessionId);
      startLocalTurn(sessionId);
      appendRef.current(sessionId, "user", query, {
        attachments: sendAttachments.length > 0 ? sendAttachments : undefined,
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
    const payload: Record<string, unknown> = {
      query: next.query,
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
    subscribedSessionsRef.current.add(sid);
    suppressChunksRef.current.delete(sid);
    startLocalTurn(sid);
    appendRef.current(sid, "user", next.query, {
      attachments: next.attachments.length > 0 ? next.attachments : undefined,
    });
    setBusy(sid, true);
    setStatus(sid, "running");
  }, []);

  const consumePendingRef = useRef(consumePending);
  consumePendingRef.current = consumePending;

  /** 停止生成：取消当前会话的运行任务，并清空本地待发队列。
   *  这里走后端 stop 硬取消路径，而不是 interrupt 软中断；软中断需要等模型流或工具
   *  到达安全点才会收尾，遇到上游阻塞时会让 UI 一直保持 busy。 */
  const stop = useCallback((sessionId: string) => {
    setPendingQueue(sessionId, []);
    const ok = sockRef.current?.stop(sessionId) ?? false;
    suppressChunksRef.current.add(sessionId);
    const book = bookFor(sessionId);
    const timing = localTurnTiming(sessionId);
    if (book.assistantId) patchRef.current(sessionId, book.assistantId, timing);
    finishLocalTurn(sessionId, ok ? "idle" : "error");
    if (!ok) {
      appendRef.current(
        sessionId,
        "error",
        "未连接到后端网关，已在本地停止等待",
        timing,
      );
    }
  }, []);

  const cancelMention = useCallback((sessionId: string, requestId?: string) => {
    const normalizedRequestId = String(requestId || "").trim();
    if (normalizedRequestId) {
      suppressedRequestIdsRef.current.add(suppressedRequestKey(sessionId, normalizedRequestId));
      // 只保留最近的取消请求，避免长时间打开页面后集合无限增长。
      while (suppressedRequestIdsRef.current.size > 200) {
        const oldest = suppressedRequestIdsRef.current.values().next().value;
        if (oldest == null) break;
        suppressedRequestIdsRef.current.delete(oldest);
      }
    }
    stop(sessionId);
    if (!normalizedRequestId) return;
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

  /** 批准计划：退出只读，后端自动起一轮执行。 */
  const approvePlan = useCallback((sessionId: string, mode: Mode, workspaceId: string) => {
    if (!sockRef.current?.planApprove(sessionId, mode, workspaceId)) return;
    setPlan(sessionId, { active: false, review: null });
    patchPlanReviewMessages(sessionId, "readonly");
    appendRef.current(sessionId, "user", "✅ 已批准计划，开始执行");
      const book = bookFor(sessionId);
      book.assistantId = null;
      book.toolMap.clear();
      book.turnStartedAt = null;
      book.awaitingAssistantAfterTool = false;
      resetDeltaBook(book);
      book.hadTeamInternal = false;
      startLocalTurn(sessionId);
      setBusy(sessionId, true);
      setStatus(sessionId, "running");
  }, []);

  /** 拒绝计划：保留 Plan 模式继续完善。 */
  const rejectPlan = useCallback((sessionId: string) => {
    sockRef.current?.planReject(sessionId);
    patchPlanReviewMessages(sessionId, "editing");
    updatePlanMap((prev) => {
      const cur = prev[sessionId] ?? { active: true, review: null };
      return {
        ...prev,
        [sessionId]: {
          active: true,
          review: cur.review ? { ...cur.review, status: "editing" } : null,
        },
      };
    });
  }, []);

  /** 拒绝计划并退出：不执行，卡片变为只读拒绝态。 */
  const rejectAndExitPlan = useCallback((sessionId: string) => {
    if (!sockRef.current?.planRejectAndExit(sessionId)) return;
    patchPlanReviewMessages(sessionId, "rejected");
    setPlan(sessionId, { active: false, review: null });
    appendRef.current(sessionId, "user", "已拒绝计划并退出 Plan 模式");
  }, []);

  /** 提交追问答案。 */
  const answerFollowup = useCallback(
    (sessionId: string, questionId: string, answers: { question_id: string; answers: string[] }[]) => {
      const ok = sockRef.current?.followupAnswer(sessionId, questionId, answers);
      if (!ok) return false;
      const question = followupMapRef.current[sessionId];
      const waitsForPermissionAck = question?.record_history === false
        && ["acp_permission", "team_control"].includes(question.origin?.type ?? "");
      if (!waitsForPermissionAck) {
        setFollowup(sessionId, null);
        if (question?.record_history !== false) {
          // Ordinary follow-ups become a new user turn.
          const book = bookFor(sessionId);
          book.assistantId = null;
          book.toolMap.clear();
          book.awaitingAssistantAfterTool = false;
          resetDeltaBook(book);
          book.hadTeamInternal = false;
          appendRef.current(sessionId, "user", `已选择：${formatFollowupAnswers(question, answers)}`);
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

  const loadHistory = useCallback(async (sessionId: string) => {
    const isLoaded = loadedSessionsRef.current.has(sessionId);
    const wasRunning = busyRef.current[sessionId] || !!bookRef.current.get(sessionId)?.assistantId;

    // 对 running/不完整的会话重新同步时，先清聚合记账，避免旧 toolMap/assistantId
    // 与新 chunk 或后端历史冲突。
    if (isLoaded && wasRunning) {
      bookFor(sessionId).toolMap.clear();
      bookFor(sessionId).assistantId = null;
      bookFor(sessionId).awaitingAssistantAfterTool = false;
      resetDeltaBook(bookFor(sessionId));
      bookFor(sessionId).hadTeamInternal = false;
    }

    subscribedSessionsRef.current.add(sessionId);
    setQueue(sessionId, "");
    if (!isLoaded && !wasRunning) {
      setMessagesMap((prev) => ({
        ...prev,
        [sessionId]: [{ id: newId(), role: "assistant" as MsgRole, text: HISTORY_LOADING_TEXT }],
      }));
    }
    try {
      // 并行取历史消息 + 当前运行态 + plan 状态。
      const [items, st, planState, todoState] = await Promise.all([
        withTimeout(api.history(sessionId), HISTORY_LOAD_TIMEOUT_MS, "history"),
        api.sessionStatus(sessionId).catch(() => null),
        api.sessionPlan(sessionId).catch(() => null),
        api.sessionTodos(sessionId).catch(() => ({ todos: [] })),
      ]);
      const list = mapHistoryItems(items);
      if (st && st.last_status === "failed" && st.last_error) {
        list.push({ id: newId(), role: "error" as MsgRole, text: st.last_error });
      }
      // 重建 planReview 卡片：定位 plan 轮——最后一条 toolCalls 含 exit_plan_mode 的
      // assistant 消息（后端历史把 exit_plan_mode 作为 tool_call 落在该轮）。把卡片挂到
      // plan 轮而非会话末尾，满足「在哪轮写就留在哪轮」；awaiting 时 plan 轮即最新轮。
      if (planState?.has_plan && planState.plan) {
        const status: PlanReview["status"] = planState.status
          ? (planState.status as PlanReview["status"])
          : planState.awaiting_approval
            ? "pending"
            : planState.active
              ? "editing"
              : "readonly";
        const review = {
          plan: planState.plan,
          planFile: planState.plan_file,
          status,
          phase: planState.phase,
        };
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
        // 已结束：用后端历史覆盖本地，补齐可能错过的 final 文本。
        setBusy(sessionId, false);
        setMessagesMap((prev) => ({
          ...prev,
          [sessionId]: preserveLocalProcessDetails(list, prev[sessionId] ?? []),
        }));
        if (st?.live === "queued") setStatus(sessionId, "queued");
        else if (st?.last_status === "failed") setStatus(sessionId, "error");
        else setStatus(sessionId, "idle");
      }

      if (planState?.has_plan) {
        setPlan(sessionId, {
          active: planState.active,
          review: null,
        });
      } else {
        setPlan(sessionId, { active: Boolean(planState?.active), review: null });
      }
      setTodos(sessionId, normalizeTodos(todoState?.todos));
      syncSubscriptions(Array.from(subscribedSessionsRef.current));
    } catch {
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
    const compactionTimer = compactionTimersRef.current[sessionId];
    if (compactionTimer) clearTimeout(compactionTimer);
    delete compactionTimersRef.current[sessionId];
    delete compactionStartedAtRef.current[sessionId];
    bookRef.current.delete(sessionId);
    subscribedSessionsRef.current.delete(sessionId);
    suppressChunksRef.current.delete(sessionId);
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
    seenGatewaySequencesRef.current.delete(sessionId);
    lastGatewaySequenceRef.current.delete(sessionId);
  }, []);

  const plan = planMap[currentSessionId] ?? { active: false, review: null };
  return {
    messages: messagesMap[currentSessionId] ?? [],
    busy: busyMap[currentSessionId] ?? false,
    queueHint: queueMap[currentSessionId] ?? "",
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
      queueHint: queueMap[sid] ?? "",
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
    approvePlan,
    rejectPlan,
    rejectAndExitPlan,
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
