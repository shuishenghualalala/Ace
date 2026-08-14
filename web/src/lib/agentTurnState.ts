import type { PlanReview, ToolCallInfo, TurnFileChangeSummary, UiMessage } from "../types";

export type ProcessTimelineItem =
  | { kind: "thinking"; id: string; content: string; done: boolean }
  | { kind: "tool"; id: string; tool: ToolCallInfo }
  | { kind: "narration"; id: string; content: string }
  | { kind: "status"; id: string; text: string }
  | { kind: "error"; id: string; text: string };

export interface AgentTurnResponse {
  id: string;
  content: string;
  streaming: boolean;
}

export interface AgentTurnPlanReview {
  id: string;
  review: PlanReview;
}

export interface AgentTurnState {
  processItems: ProcessTimelineItem[];
  responses: AgentTurnResponse[];
  planReviews: AgentTurnPlanReview[];
  fileChanges: TurnFileChangeSummary[];
  toolCount: number;
  commandCount: number;
  hasThinking: boolean;
  showTyping: boolean;
  turnStartedAt?: number;
  turnDurationMs?: number;
  timestamp?: number;
  completed: boolean;
}

export type AgentTurnEvent =
  | { type: "thinking"; id: string; content: string; done: boolean }
  | { type: "tool"; id: string; tool: ToolCallInfo }
  | { type: "narration"; id: string; content: string }
  | { type: "response"; id: string; content: string; streaming: boolean }
  | { type: "status"; id: string; text: string }
  | { type: "error"; id: string; text: string }
  | { type: "plan_review"; id: string; review: PlanReview }
  | {
      type: "timing";
      turnStartedAt?: number;
      turnDurationMs?: number;
      timestamp?: number;
    }
  | { type: "typing"; visible: boolean }
  | { type: "complete"; completed: boolean };

export const emptyAgentTurnState = (): AgentTurnState => ({
  processItems: [],
  responses: [],
  planReviews: [],
  fileChanges: [],
  toolCount: 0,
  commandCount: 0,
  hasThinking: false,
  showTyping: false,
  completed: false,
});

function overlapConcat(left: string, right: string): string {
  const max = Math.min(left.length, right.length);
  for (let size = max; size > 0; size -= 1) {
    if (left.slice(-size) === right.slice(0, size)) {
      return `${left}${right.slice(size)}`;
    }
  }
  return `${left}${right}`;
}

/** Merge delta, cumulative and overlapping streaming frames without duplication. */
export function mergeStreamingText(
  existing: string | undefined,
  incoming: string | undefined,
  mode: "append" | "snapshot" = "append",
): string {
  const left = String(existing || "");
  const right = String(incoming || "");
  if (!right) return left;
  if (!left) return right;
  if (left === right || left.endsWith(right) || left.includes(right)) return left;
  if (right.startsWith(left) || right.includes(left)) return right;
  if (mode === "snapshot") return right;
  return overlapConcat(left, right);
}

export function mergeAgentToolCalls(
  existing: ToolCallInfo[] | undefined,
  incoming: ToolCallInfo[] | undefined,
): ToolCallInfo[] {
  const merged = [...(existing || [])];
  for (const tool of incoming || []) {
    const key = tool.toolCallId || `${tool.name}_${merged.length}`;
    const index = merged.findIndex((item) => item.toolCallId === key);
    if (index < 0) {
      merged.push({ ...tool, toolCallId: key });
      continue;
    }
    const previous = merged[index];
    merged[index] = {
      ...previous,
      ...tool,
      toolCallId: key,
      uiLabel: tool.uiLabel || previous.uiLabel,
      args: tool.args || previous.args,
      result: tool.result || previous.result,
      startedAt: tool.startedAt || previous.startedAt || 0,
      duration: tool.duration ?? previous.duration,
      status: tool.status === "running" && previous.status !== "running"
        ? previous.status
        : tool.status,
    };
  }
  return merged;
}

/** Pure reducer shared by ACP and Team turn projections. */
export function reduceAgentTurnEvent(state: AgentTurnState, event: AgentTurnEvent): AgentTurnState {
  if (event.type === "timing") {
    return {
      ...state,
      turnStartedAt: state.turnStartedAt ?? event.turnStartedAt,
      turnDurationMs: event.turnDurationMs ?? state.turnDurationMs,
      timestamp: state.timestamp ?? event.timestamp,
    };
  }
  if (event.type === "typing") return { ...state, showTyping: event.visible };
  if (event.type === "complete") return { ...state, completed: event.completed };
  if (event.type === "plan_review") {
    return { ...state, planReviews: [...state.planReviews, { id: event.id, review: event.review }] };
  }
  if (event.type === "response") {
    return {
      ...state,
      responses: [...state.responses, {
        id: event.id,
        content: event.content,
        streaming: event.streaming,
      }],
      showTyping: false,
    };
  }
  if (event.type === "tool") {
    const processItems = [...state.processItems];
    const index = processItems.findIndex((item) => item.kind === "tool" && item.tool.toolCallId === event.tool.toolCallId);
    const previous = index >= 0 && processItems[index].kind === "tool"
      ? processItems[index].tool
      : undefined;
    const tool = mergeAgentToolCalls(previous ? [previous] : [], [event.tool])[0];
    const item: ProcessTimelineItem = { kind: "tool", id: tool.toolCallId, tool };
    if (index >= 0) processItems[index] = item;
    else processItems.push(item);
    const tools = processItems
      .filter((candidate): candidate is Extract<ProcessTimelineItem, { kind: "tool" }> => candidate.kind === "tool")
      .map((candidate) => candidate.tool);
    const commandCount = tools.filter((tool) => ["terminal", "process", "bash"].includes(tool.name.toLowerCase())).length;
    return { ...state, processItems, toolCount: tools.length, commandCount };
  }
  if (event.type === "thinking") {
    const index = state.processItems.findIndex((item) => item.kind === "thinking" && item.id === event.id);
    const next: ProcessTimelineItem = index >= 0
      ? {
          kind: "thinking",
          id: event.id,
          content: mergeStreamingText(
            (state.processItems[index] as Extract<ProcessTimelineItem, { kind: "thinking" }>).content,
            event.content,
          ),
          done: event.done,
        }
      : { kind: "thinking", id: event.id, content: event.content, done: event.done };
    const processItems = [...state.processItems];
    if (index >= 0) processItems[index] = next;
    else processItems.push(next);
    return { ...state, processItems, hasThinking: true };
  }
  const item: ProcessTimelineItem = event.type === "narration"
    ? { kind: "narration", id: event.id, content: event.content }
    : event.type === "status"
      ? { kind: "status", id: event.id, text: event.text }
      : { kind: "error", id: event.id, text: event.text };
  return { ...state, processItems: [...state.processItems, item] };
}

function lastAssistantTextId(messages: UiMessage[]): string | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === "assistant" && message.text?.trim()) return message.id;
  }
  return null;
}

/** Project persisted or live UiMessages into the same ACP/Team bubble model. */
export function buildAgentTurnState(messages: UiMessage[], isStreaming: boolean): AgentTurnState {
  const finalTextId = lastAssistantTextId(messages)
    ?? [...messages].reverse().find((message) => message.text?.trim())?.id
    ?? null;
  let state = emptyAgentTurnState();

  messages.forEach((message, index) => {
    if (message.turnFileChanges?.length) {
      const merged = new Map(state.fileChanges.map((file) => [file.path, file]));
      for (const file of message.turnFileChanges) merged.set(file.path, file);
      state = { ...state, fileChanges: Array.from(merged.values()) };
    }
    state = reduceAgentTurnEvent(state, {
      type: "timing",
      turnStartedAt: message.turnStartedAt,
      turnDurationMs: message.turnDurationMs,
      timestamp: message.timestamp,
    });
    if (message.role === "status") {
      state = reduceAgentTurnEvent(state, { type: "status", id: message.id, text: message.text });
      return;
    }
    if (message.role === "error") {
      state = reduceAgentTurnEvent(state, { type: "error", id: message.id, text: message.text });
      return;
    }

    const text = message.text?.trim() ?? "";
    const thinking = message.thinking?.trim() ?? "";
    const tools = mergeAgentToolCalls([], message.toolCalls);
    const thinkingIsOnlyAnswer = !isStreaming
      && !text
      && Boolean(thinking)
      && tools.length === 0
      && !message.planReview;
    if (thinking && !thinkingIsOnlyAnswer && (!text || thinking !== text)) {
      state = reduceAgentTurnEvent(state, {
        type: "thinking",
        id: `${message.id}-thinking`,
        content: message.thinking!,
        done: !isStreaming,
      });
    }
    for (const tool of tools) {
      state = reduceAgentTurnEvent(state, { type: "tool", id: tool.toolCallId, tool });
    }
    if (message.planReview) {
      state = reduceAgentTurnEvent(state, {
        type: "plan_review",
        id: `${message.id}-plan`,
        review: message.planReview,
      });
    }

    const displayText = thinkingIsOnlyAnswer ? message.thinking! : message.text;
    if (displayText) {
      state = reduceAgentTurnEvent(state, message.id === finalTextId
        ? {
            type: "response",
            id: `${message.id}-text`,
            content: displayText,
            streaming: isStreaming && !thinkingIsOnlyAnswer,
          }
        : { type: "narration", id: `${message.id}-narration`, content: displayText });
    } else if (isStreaming && index === messages.length - 1) {
      state = reduceAgentTurnEvent(state, { type: "typing", visible: true });
    }
  });

  return reduceAgentTurnEvent(state, { type: "complete", completed: !isStreaming });
}
