import type { MsgRole, ToolCallInfo, TurnFileChangeSummary, UiMessage } from "../types";
import { backendDurationToMs, backendSecondsToMs } from "./backendTime";
import { isDuplicateAssistantOfTeamResult, mergeTeamInternalMessage } from "./teamMessageMerge";

export interface BackendHistoryItem {
  role: string;
  content: string;
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
    result: tc.result ?? "",
    status: tc.status ?? "done",
    startedAt: backendSecondsToMs(tc.started_at) ?? 0,
    duration: backendDurationToMs(tc.duration),
  }));
}

/** 把网关历史条目映射为前端 UiMessage（含秒→毫秒换算）。 */
export function mapHistoryItem(item: BackendHistoryItem): UiMessage {
  const role = item.role as MsgRole;
  const base: UiMessage = {
    id: newId(),
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

export function mapHistoryItems(items: BackendHistoryItem[]): UiMessage[] {
  const messages: UiMessage[] = [];
  for (const item of items) {
    const message = mapHistoryItem(item);
    if (message.role !== "team_internal") {
      messages.push(message);
      continue;
    }
    messages.splice(0, messages.length, ...mergeTeamInternalMessage(
      messages,
      message,
      { append: message.eventType === "team_stream" },
    ));
  }
  return messages;
}

export function mergeHistoryWithLiveMessages(
  history: UiMessage[],
  live: UiMessage[],
): UiMessage[] {
  return live.reduce(
    (messages, message) => message.role === "team_internal"
      ? mergeTeamInternalMessage(messages, message)
      : messages.some((existing) => isDuplicateAssistantOfTeamResult(existing, message))
        ? messages
      : [...messages, message],
    history,
  );
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
