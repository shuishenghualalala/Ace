import type { MsgRole, ToolCallInfo, TurnFileChangeSummary, UiMessage } from "../types";
import { backendDurationToMs, backendSecondsToMs } from "./backendTime";
import { isDuplicateAssistantOfTeamResult, mergeTeamInternalMessage } from "./teamMessageMerge";

export interface ToolResultMetadata {
  code?: string;
  details?: { exit_confirmed?: boolean };
}
export function toolExitUnconfirmed(metadata?: ToolResultMetadata) {
  return metadata?.code === "tool_exit_unconfirmed" || metadata?.details?.exit_confirmed === false;
}

export interface BackendHistoryItem {
  role: string;
  content: string;
  history_id?: string;
  outcome?: unknown;
  timestamp?: number;
  turn_started_at?: number;
  turn_duration?: number;
  thinking?: string;
  source_session_id?: string;
  agent_id?: string;
  agent_name?: string;
  agent_role?: string;
  agent_tone?: number;
  is_leader?: boolean;
  event_type?: string;
  node_id?: string;
  mention_from?: string;
  mention_to?: string[];
  mention_intent?: string;
  communication_kind?: string;
  communication_status?: string;
  request_id?: string;
  reply_to?: string;
  communication_request_text?: string;
  display_mode?: string;
  collapsed_title?: string;
  process_text?: string;
  artifacts?: UiMessage["artifacts"];
  turn_file_changes?: Array<Partial<TurnFileChangeSummary> & { path?: string }>;
  tool_calls?: Array<{
    id: string;
    name: string;
    ui_label?: string;
    arguments: Record<string, unknown>;
    started_at?: number;
    duration?: number;
    result?: string;
    result_metadata?: ToolResultMetadata;
    status?: "generating" | "running" | "done" | "error";
  }>;
}

function nonNegativeCount(value: unknown): number {
  const count = Number(value);
  return Number.isFinite(count) ? Math.max(0, count) : 0;
}

export function normalizeTurnFileChanges(raw: unknown): TurnFileChangeSummary[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const files = raw.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const value = item as Record<string, unknown>;
    const path = String(value.path || "").trim();
    if (!path || isPlanDocumentPath(path)) return [];
    const status = value.status === "added" || value.status === "deleted" ? value.status : "modified";
    return [{
      path,
      name: String(value.name || path.split(/[\\/]/).pop() || path),
      added: nonNegativeCount(value.added),
      removed: nonNegativeCount(value.removed),
      status,
      ...(value.binary === true ? { binary: true } : {}),
    } satisfies TurnFileChangeSummary];
  });
  return files.length > 0 ? files : undefined;
}

function isPlanDocumentPath(path: string): boolean {
  const normalized = String(path || "").replace(/\\/g, "/");
  return /(^|\/)\.crew\/plans\//i.test(normalized)
    || (/\/plans\//i.test(normalized) && /\/plan_[^/]+\.md$/i.test(normalized));
}

let _seq = 0;
const newId = () => `h${Date.now()}_${_seq++}`;

function mapToolCalls(
  toolCalls: NonNullable<BackendHistoryItem["tool_calls"]>,
): ToolCallInfo[] {
  return toolCalls.map((tc) => ({
    toolCallId: tc.id,
    name: tc.name,
    uiLabel: tc.ui_label,
    args: JSON.stringify(tc.arguments),
    ...(toolExitUnconfirmed(tc.result_metadata) ? { exitUnconfirmed: true } : {}),
    result: tc.result || (toolExitUnconfirmed(tc.result_metadata) ? "工具实际退出尚未确认。" : ""),
    status: toolExitUnconfirmed(tc.result_metadata) ? "error" : tc.status ?? "done",
    startedAt: backendSecondsToMs(tc.started_at) ?? 0,
    duration: tc.duration == null ? undefined : backendDurationToMs(tc.duration) || undefined,
  }));
}

/** 把网关历史条目映射为前端 UiMessage（含秒→毫秒换算）。 */
export function mapHistoryItem(item: BackendHistoryItem): UiMessage {
  const role: MsgRole =
    item.role === "assistant" || item.role === "user" || item.role === "team_internal"
      ? item.role
      : item.role === "error" ? "error" : "status";
  const base: UiMessage = {
    id: role === "user" && item.history_id?.startsWith("followup:") ? item.history_id : newId(),
    role,
    text: item.content,
    timestamp: backendSecondsToMs(item.timestamp),
    turnStartedAt: backendSecondsToMs(item.turn_started_at),
    turnDurationMs: backendDurationToMs(item.turn_duration) || undefined,
    thinking: item.thinking,
    sourceSessionId: item.source_session_id,
    agentId: item.agent_id,
    agentName: item.agent_name,
    agentRole: item.agent_role,
    agentTone: item.agent_tone,
    isLeader: item.is_leader,
    eventType: item.event_type,
    nodeId: item.node_id,
    mentionFrom: item.mention_from,
    mentionTo: item.mention_to,
    mentionIntent: item.mention_intent,
    communicationKind: item.communication_kind,
    communicationStatus: item.communication_status,
    requestId: item.request_id,
    replyTo: item.reply_to,
    communicationRequestText: item.communication_request_text,
    displayMode: item.display_mode,
    collapsedTitle: item.collapsed_title,
    processText: item.process_text,
    artifacts: item.artifacts,
    turnFileChanges: normalizeTurnFileChanges(item.turn_file_changes),
  };
  if ((role === "assistant" || role === "team_internal") && item.tool_calls && item.tool_calls.length > 0) {
    base.toolCalls = mapToolCalls(item.tool_calls);
  }
  return base;
}

const outcomeReasonText = new Map([
  ["output_limit", "回复达到模型输出上限，已保留生成的内容。"],
  ["max_iterations", "回合达到执行步数上限，已保留当前结果。"],
  ["stream_interrupted", "模型响应中断，已保留收到的内容。"],
  ["tool_reconciliation_required", "请先核对工具执行结果，再决定是否继续。"],
]);

function outcomeText(value: unknown): string | undefined {
  return typeof value === "string" ? value.trim().slice(0, 800) || undefined : undefined;
}

export function turnOutcomeNotice(
  value: unknown,
  requestId?: string,
  fallbackMessage?: unknown,
): UiMessage | undefined {
  if (!value || typeof value !== "object") return undefined;
  const outcome = value as Record<string, unknown>;
  if (outcome.status !== "partial" && outcome.status !== "failed") return undefined;
  const request = requestId || (typeof outcome.request_id === "string" ? outcome.request_id : undefined);
  const detail = outcomeText(outcome.message)
    ?? (typeof outcome.reason === "string" ? outcomeReasonText.get(outcome.reason) : undefined)
    ?? outcomeText(fallbackMessage);
  const failed = outcome.status === "failed";
  const title = failed ? "本轮执行失败。" : "回复未完成。";
  return {
    id: request ? "turn-outcome:" + request : newId(),
    role: failed ? "error" : "status",
    requestId: request,
    turnOutcome: outcome.status,
    text: detail ? title + detail : title,
  };
}

export function mapHistoryItems(items: BackendHistoryItem[]): UiMessage[] {
  const messages: UiMessage[] = [];
  const notices = new Set<string>();
  for (const item of items) {
    const message = mapHistoryItem(item);
    if (message.role === "team_internal") {
      messages.splice(0, messages.length, ...mergeTeamInternalMessage(
        messages,
        message,
        { append: message.eventType === "team_stream" },
      ));
      continue;
    }
    messages.push(message);
    const notice = turnOutcomeNotice(item.outcome, item.request_id);
    if (notice && !notices.has(notice.id)) {
      messages.push(notice);
      notices.add(notice.id);
    }
  }
  return messages;
}

export function settleFollowupAnswer(messages: UiMessage[], body: {
  question_id?: string; status?: string; answer_history_id?: string; answer_text?: string; record_history?: boolean;
}): UiMessage[] {
  if (!body.question_id || body.record_history === false) return messages;
  const id = `followup:${body.question_id}`;
  const existing = messages.find(message => message.role === "user" && message.id === id);
  if (body.status === "expired" || body.status === "cancelled") {
    return existing?.pendingFollowupAnswer ? messages.filter(message => message !== existing) : messages;
  }
  if (body.status !== "resolved") return messages;
  const canonical = body.answer_history_id === id && typeof body.answer_text === "string" && body.answer_text.trim()
    ? body.answer_text : undefined;
  if (!existing) return canonical ? [...messages, { id, role: "user", text: canonical }] : messages;
  const text = canonical ?? existing.text;
  if (!existing.pendingFollowupAnswer && text === existing.text) return messages;
  return messages.map(message => message === existing ? { ...message, text, pendingFollowupAnswer: false } : message);
}

export function mergeHistoryWithLiveMessages(
  history: UiMessage[],
  live: UiMessage[],
): UiMessage[] {
  const savedAnswers = new Set(history.filter(m => m.role === "user" && m.id.startsWith("followup:")).map(m => m.id));
  const shared = live.filter(m => savedAnswers.has(m.id));
  let nextAnswer = 0;
  return live.reduce((messages, message) => {
    const anchor = shared[nextAnswer];
    if (message === anchor) {
      nextAnswer += 1;
      return messages;
    }
    if ((message.turnOutcome || (message.role === "user" && message.id.startsWith("followup:")))
      && messages.some(existing => existing.id === message.id)) return messages;
    if (message.role !== "team_internal" && messages.some(existing => isDuplicateAssistantOfTeamResult(existing, message))) return messages;
    const merged = message.role === "team_internal"
      ? mergeTeamInternalMessage(messages, message) : [...messages, message];
    // The answer may already be durable while the preceding live segment isn't.
    // Team messages reconciled in place keep their original timeline position.
    if (!anchor || merged.at(-1) !== message) return merged;
    const at = merged.findIndex(row => row.id === anchor.id);
    return [...merged.slice(0, at), message, ...merged.slice(at, -1)];
  }, history);
}

function sameAssistantTurn(history: UiMessage, local: UiMessage): boolean {
  if (history.role !== "assistant" || local.role !== "assistant") return false;
  const historyText = (history.text || "").trim();
  const localText = (local.text || "").trim();
  if (!historyText || !localText) return historyText === localText;
  return historyText === localText || historyText.includes(localText) || localText.includes(historyText);
}

/** Preserve a just-finished local timeline when session persistence trails final. */
export function preserveLocalProcessDetails(
  history: UiMessage[],
  local: UiMessage[],
): UiMessage[] {
  const candidates = local.filter((message) => message.role === "assistant");
  let cursor = 0;
  return history.map((message) => {
    if (message.role !== "assistant") return message;
    let matchedIndex = -1;
    for (let index = cursor; index < candidates.length; index += 1) {
      if (sameAssistantTurn(message, candidates[index])) {
        matchedIndex = index;
        break;
      }
    }
    if (matchedIndex < 0) return message;
    cursor = matchedIndex + 1;
    const matched = candidates[matchedIndex];
    return {
      ...message,
      thinking: message.thinking || matched.thinking,
      toolCalls: message.toolCalls?.length ? message.toolCalls : matched.toolCalls,
    };
  });
}
