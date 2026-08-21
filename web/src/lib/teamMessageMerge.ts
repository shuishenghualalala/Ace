import type { UiMessage } from "../types";
import { mergeAgentToolCalls, mergeStreamingText } from "./agentTurnState";

function mergeThinking(existing?: string, incoming?: string, append = false): string | undefined {
  if (!incoming) return existing;
  return mergeStreamingText(existing, incoming, append ? "append" : "snapshot");
}

function isTeamNodeResult(message: UiMessage): boolean {
  return [
    "team_submit",
    "team_summary",
    "team_review",
    "team_decision",
  ].includes(message.eventType || "");
}

function compactText(value?: string): string {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function mergedTeamTurnTiming(
  existing: UiMessage,
  incoming: UiMessage,
): Pick<UiMessage, "turnStartedAt" | "turnDurationMs"> {
  const turnStartedAt = existing.turnStartedAt ?? incoming.turnStartedAt;
  const persistedDuration = incoming.turnDurationMs ?? existing.turnDurationMs;
  const turnDurationMs = !isTeamStream(incoming) && turnStartedAt != null
    ? Math.max(0, persistedDuration ?? (incoming.timestamp || 0) - turnStartedAt)
    : persistedDuration;
  return {
    ...(turnStartedAt != null ? { turnStartedAt } : {}),
    ...(turnDurationMs != null ? { turnDurationMs } : {}),
  };
}

export function isDuplicateAssistantOfTeamResult(existing: UiMessage, incoming: UiMessage): boolean {
  const assistant = existing.role === "assistant" ? existing : incoming.role === "assistant" ? incoming : null;
  const team = existing.role === "team_internal" ? existing : incoming.role === "team_internal" ? incoming : null;
  if (!assistant || !team || !isTeamNodeResult(team)) return false;
  if (assistant.requestId && team.requestId && assistant.requestId === team.requestId) {
    return team.eventType === "team_summary";
  }
  const assistantText = compactText(assistant.text);
  const teamText = compactText(team.text);
  if (!assistantText || !teamText) return false;
  return assistantText === teamText || assistantText.includes(teamText) || teamText.includes(assistantText);
}

function isTeamStream(message: UiMessage): boolean {
  return message.eventType === "team_stream";
}

function isTeamPlanningProgress(message: UiMessage): boolean {
  return message.eventType === "team_planning_progress";
}

function isUserMentionAnswer(message: UiMessage): boolean {
  return message.communicationKind === "user_mention_answer" && Boolean(message.requestId);
}

function sameTeamRequest(existing: UiMessage, incoming: UiMessage): boolean {
  const existingRequestId = String(existing.requestId || "").trim();
  const incomingRequestId = String(incoming.requestId || "").trim();
  if (existingRequestId || incomingRequestId) {
    return Boolean(existingRequestId && incomingRequestId && existingRequestId === incomingRequestId);
  }
  return Boolean(
    existing.sourceSessionId
    && incoming.sourceSessionId
    && existing.sourceSessionId === incoming.sourceSessionId,
  );
}

function isDuplicateTeamEvent(existing: UiMessage, incoming: UiMessage): boolean {
  return existing.role === "team_internal"
    && incoming.role === "team_internal"
    && !isTeamStream(existing)
    && !isTeamStream(incoming)
    && existing.eventType === incoming.eventType
    && existing.nodeId === incoming.nodeId
    && (existing.agentId || existing.mentionFrom) === (incoming.agentId || incoming.mentionFrom)
    && sameTeamRequest(existing, incoming)
    && existing.text.trim() === incoming.text.trim();
}

function isApproveDecision(message: UiMessage): boolean {
  if (message.eventType !== "team_decision") return false;
  if (message.mentionIntent === "approve") return true;
  const text = (message.text || "").trim();
  return text === "审阅通过，继续后续流程。" || text === "审阅通过，开始后续流程。";
}

function shouldSuppressApproveDecision(existing: UiMessage, incoming: UiMessage): boolean {
  if (!isApproveDecision(incoming)) return false;
  if (existing.eventType !== "team_review") return false;
  return Boolean(existing.nodeId && incoming.nodeId && existing.nodeId === incoming.nodeId);
}

function teamTurnKey(message: UiMessage): string {
  const source = String(message.sourceSessionId || "").trim();
  if (!source) return "";
  const marker = "::turn::";
  if (!source.includes(marker)) return source;
  const [parent, rest] = source.split(marker, 2);
  const requestId = rest.split("::", 1)[0];
  return requestId ? `${parent}${marker}${requestId}` : source;
}

function matchesTeamNode(existing: UiMessage, incoming: UiMessage): boolean {
  if (existing.role !== "team_internal") return false;
  if (!isTeamStream(existing) && !isTeamNodeResult(existing) && !isTeamPlanningProgress(existing)) return false;
  if (incoming.nodeId && existing.nodeId === incoming.nodeId) {
    const existingTurn = teamTurnKey(existing);
    const incomingTurn = teamTurnKey(incoming);
    return Boolean(existingTurn && incomingTurn && existingTurn === incomingTurn);
  }
  return Boolean(
    !incoming.nodeId &&
    existing.sourceSessionId &&
    incoming.sourceSessionId &&
    existing.sourceSessionId === incoming.sourceSessionId &&
    existing.agentId === incoming.agentId,
  );
}

function findMatchingTeamNode(messages: UiMessage[], incoming: UiMessage): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (matchesTeamNode(messages[index], incoming)) return index;
  }
  return -1;
}

export function mergeTeamInternalMessage(
  messages: UiMessage[],
  incoming: UiMessage,
  options: { append?: boolean } = {},
): UiMessage[] {
  if (incoming.role !== "team_internal") return [...messages, incoming];
  const withoutDuplicateAssistant = isTeamNodeResult(incoming)
    ? messages.filter((message) => !isDuplicateAssistantOfTeamResult(message, incoming))
    : messages;
  if (withoutDuplicateAssistant.some((message) => isDuplicateTeamEvent(message, incoming))) {
    return withoutDuplicateAssistant;
  }
  if (isUserMentionAnswer(incoming)) {
    const communicationIndex = withoutDuplicateAssistant.findIndex((message) =>
      isUserMentionAnswer(message)
      && message.requestId === incoming.requestId
      && message.agentId === incoming.agentId,
    );
    if (communicationIndex >= 0) {
      const matched = withoutDuplicateAssistant[communicationIndex];
      if (isTeamStream(incoming) && !isTeamStream(matched)) {
        return [
          ...withoutDuplicateAssistant.slice(0, communicationIndex),
          { ...matched, ...incoming },
          ...withoutDuplicateAssistant.slice(communicationIndex + 1),
        ];
      }
      if (!isTeamStream(incoming)) {
        const matchedProcessText = isTeamStream(matched)
          && (matched.text || '').trim()
          && (matched.text || '').trim() !== (incoming.text || '').trim()
          ? matched.processText || matched.text
          : incoming.processText;
        return [
          ...withoutDuplicateAssistant.slice(0, communicationIndex),
          {
            ...matched,
            ...incoming,
            displayMode: incoming.displayMode || matched.displayMode,
            collapsedTitle: matched.collapsedTitle || incoming.collapsedTitle,
            processText: matchedProcessText,
            thinking: mergeThinking(matched.thinking, incoming.thinking),
            toolCalls: mergeAgentToolCalls(matched.toolCalls, incoming.toolCalls),
            ...mergedTeamTurnTiming(matched, incoming),
          },
          ...withoutDuplicateAssistant.slice(communicationIndex + 1),
        ];
      }
      // 流式 direct mention 继续进入下面的 append 合并，不覆盖已有过程。
    }
  }
  const matchingIndex = findMatchingTeamNode(withoutDuplicateAssistant, incoming);

  if (isTeamPlanningProgress(incoming) && matchingIndex >= 0) {
    return [
        ...withoutDuplicateAssistant.slice(0, matchingIndex),
        {
          ...withoutDuplicateAssistant[matchingIndex],
          ...incoming,
          ...mergedTeamTurnTiming(withoutDuplicateAssistant[matchingIndex], incoming),
        },
      ...withoutDuplicateAssistant.slice(matchingIndex + 1),
    ];
  }

  if (isTeamNodeResult(incoming) && matchingIndex >= 0) {
    const matched = withoutDuplicateAssistant[matchingIndex];
    if (shouldSuppressApproveDecision(matched, incoming)) {
      return withoutDuplicateAssistant;
    }
    if (!isTeamStream(matched)) return [...withoutDuplicateAssistant, incoming];
    const displayMode = incoming.displayMode || "chat";
    const matchedProcessText = (matched.processText || matched.text || "").trim();
    const incomingText = (incoming.text || "").trim();
    const incomingProcessText = (incoming.processText || "").trim();
    const preservedProcessText = matchedProcessText && matchedProcessText !== incomingText
      ? (matched.processText || matched.text)
      : incomingProcessText && incomingProcessText !== incomingText
        ? incoming.processText
        : undefined;
    const replacement = ["stream", "collapsible"].includes(matched.displayMode || "") && (matched.text || "").trim()
      ? {
          ...matched,
          ...incoming,
          displayMode,
          collapsedTitle: matched.collapsedTitle || incoming.collapsedTitle || `${matched.agentRole || "当前节点"} 的执行过程`,
          processText: preservedProcessText,
          thinking: mergeThinking(matched.thinking, incoming.thinking),
          toolCalls: mergeAgentToolCalls(matched.toolCalls, incoming.toolCalls),
          ...mergedTeamTurnTiming(matched, incoming),
        }
      : {
          ...matched,
          ...incoming,
          displayMode,
          thinking: mergeThinking(matched.thinking, incoming.thinking),
          toolCalls: mergeAgentToolCalls(matched.toolCalls, incoming.toolCalls),
          ...mergedTeamTurnTiming(matched, incoming),
        };
    return [
      ...withoutDuplicateAssistant.slice(0, matchingIndex),
      replacement,
      ...withoutDuplicateAssistant.slice(matchingIndex + 1),
    ];
  }

  if (
    options.append &&
    matchingIndex >= 0 &&
    isTeamStream(incoming) &&
    isTeamStream(withoutDuplicateAssistant[matchingIndex]) &&
    ["collapsible", "stream"].includes(withoutDuplicateAssistant[matchingIndex].displayMode || "")
  ) {
    const matched = withoutDuplicateAssistant[matchingIndex];
    return [
      ...withoutDuplicateAssistant.slice(0, matchingIndex),
      {
        ...matched,
        text: mergeStreamingText(matched.text, incoming.text),
        thinking: mergeThinking(matched.thinking, incoming.thinking, true),
        toolCalls: mergeAgentToolCalls(matched.toolCalls, incoming.toolCalls),
        processText: mergeStreamingText(matched.processText, incoming.processText),
        turnStartedAt: matched.turnStartedAt ?? incoming.turnStartedAt,
        turnDurationMs: incoming.turnDurationMs ?? matched.turnDurationMs,
        timestamp: incoming.timestamp,
      },
      ...withoutDuplicateAssistant.slice(matchingIndex + 1),
    ];
  }

  return [...withoutDuplicateAssistant, incoming];
}
