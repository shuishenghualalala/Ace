import type {
  FeatureEventContext,
  FeatureEventEffect,
  FeatureEventRegistry,
} from "../lib/feature-event-dispatcher";
import { backendDurationToMs, backendSecondsToMs } from "../lib/backendTime";
import { normalizeChunkToolCalls, normalizeTeamText } from "../lib/chunkNormalize";
import { normalizeTurnFileChanges } from "../lib/historyMap";
import { mergeTeamInternalMessage } from "../lib/teamMessageMerge";
import type { UiMessage } from "../types";

function buildTeamInternalMessage(payload: unknown, ctx: FeatureEventContext): UiMessage {
  const body =
    payload && typeof payload === "object" ? (payload as Record<string, unknown>) : {};
  return {
    id: ctx.newId(),
    role: "team_internal",
    text: normalizeTeamText(body.text),
    sourceSessionId: body.source_session_id as string | undefined,
    agentId: body.agent_id as string | undefined,
    agentName: body.agent_name as string | undefined,
    agentRole: body.agent_role as string | undefined,
    agentTone: typeof body.agent_tone === "number" ? body.agent_tone : undefined,
    isLeader: body.is_leader as boolean | undefined,
    eventType: body.event_type as string | undefined,
    nodeId: body.node_id as string | undefined,
    mentionFrom: body.mention_from as string | undefined,
    mentionTo: body.mention_to as string[] | undefined,
    mentionIntent: body.mention_intent as string | undefined,
    communicationKind: body.communication_kind as string | undefined,
    communicationStatus: body.communication_status as string | undefined,
    requestId: body.request_id as string | undefined,
    replyTo: body.reply_to as string | undefined,
    communicationRequestText: body.communication_request_text as string | undefined,
    displayMode: body.display_mode as string | undefined,
    collapsedTitle: body.collapsed_title as string | undefined,
    thinking: normalizeTeamText(body.thinking),
    toolCalls: normalizeChunkToolCalls(body.tool_calls),
    artifacts: body.artifacts as UiMessage["artifacts"],
    turnFileChanges: normalizeTurnFileChanges(body.turn_file_changes),
    timestamp: backendSecondsToMs(body.timestamp as number | null | undefined) ?? ctx.now,
    turnStartedAt: backendSecondsToMs(body.turn_started_at as number | null | undefined) ?? ctx.startLocalTurn(),
    turnDurationMs:
      body.turn_duration != null
        ? backendDurationToMs(body.turn_duration as number)
        : undefined,
  };
}

export function installTeamFeatureHandlers(registry: FeatureEventRegistry): () => void {
  return registry.register({
    feature: "team",
    event: "internal_message",
    version: 1,
    handler(payload, ctx): FeatureEventEffect {
      const incoming = buildTeamInternalMessage(payload, ctx);
      const body =
        payload && typeof payload === "object"
          ? (payload as Record<string, unknown>)
          : {};
      return {
        messages: (prev) =>
          mergeTeamInternalMessage(prev, incoming, { append: Boolean(body.append) }),
        statusHint: "running",
        queueHint: "",
        hadTeamInternal: true,
      };
    },
  });
}

export function installKanbanFeatureHandlers(registry: FeatureEventRegistry): () => void {
  return registry.register({
    feature: "kanban",
    event: "workflow_progress",
    version: 1,
    handler(): FeatureEventEffect | null {
      // Web 端当前不渲染 workflow_progress 帧；注册空 handler 以避免未命中 warn，
      // 同时保持与原代码“静默忽略”一致的行为。
      return null;
    },
  });
}

function buildSubagentLifecycleMessage(
  event: "run_started" | "run_settled",
  payload: unknown,
  ctx: FeatureEventContext,
): UiMessage {
  const body =
    payload && typeof payload === "object" ? (payload as Record<string, unknown>) : {};
  const label = String(body.label || "subagent").trim() || "subagent";
  const stopReason = String(body.stop_reason || "").trim();
  const text =
    event === "run_started"
      ? `子智能体「${label}」已启动`
      : `子智能体「${label}」已结束${stopReason ? `（${stopReason}）` : ""}`;
  return {
    id: ctx.newId(),
    role: "status",
    text,
    subagentId: String(body.subagent_id || "").trim() || undefined,
    subagentState: event === "run_started" ? "started" : "settled",
    subagentLabel: label,
    subagentProvider: String(body.provider || "").trim() || undefined,
    subagentStopReason: stopReason || undefined,
    timestamp: ctx.now,
  };
}

function buildSubagentActivationMessage(
  event: "activation_running" | "activation_ready" | "activation_idle",
  payload: unknown,
  ctx: FeatureEventContext,
): UiMessage {
  const body =
    payload && typeof payload === "object" ? (payload as Record<string, unknown>) : {};
  const label = String(body.label || "subagent").trim() || "subagent";
  const state = event.replace("activation_", "") as "running" | "ready" | "idle";
  const text = state === "running"
    ? `子智能体「${label}」运行中`
    : state === "ready"
      ? `子智能体「${label}」等待继续`
      : `子智能体「${label}」已空闲`;
  return {
    id: ctx.newId(),
    role: "status",
    text,
    subagentId: String(body.subagent_id || "").trim() || undefined,
    subagentState: state,
    subagentLabel: label,
    subagentProvider: String(body.provider || "").trim() || undefined,
    timestamp: ctx.now,
  };
}

export function installSubagentFeatureHandlers(registry: FeatureEventRegistry): () => void {
  const disposers = (["run_started", "run_settled"] as const).map((event) =>
    registry.register({
      feature: "subagent",
      event,
      version: 1,
      handler(payload, ctx): FeatureEventEffect {
        const message = buildSubagentLifecycleMessage(event, payload, ctx);
        const stopReason = message.subagentStopReason;
        return {
          messages: (prev) => [...prev, message],
          statusHint:
            event === "run_started"
              ? "running"
              : stopReason === "error" || stopReason === "timed_out"
                ? "error"
                : "idle",
          queueHint: "",
        };
      },
    }),
  );
  const activationDisposers = ([
    "activation_running",
    "activation_ready",
    "activation_idle",
  ] as const).map((event) =>
    registry.register({
      feature: "subagent",
      event,
      version: 1,
      handler(payload, ctx): FeatureEventEffect {
        const message = buildSubagentActivationMessage(event, payload, ctx);
        return {
          messages: (prev) => [...prev, message],
          statusHint: event === "activation_running" ? "running" : "idle",
          queueHint: "",
        };
      },
    }),
  );
  return () => {
    disposers.forEach((dispose) => dispose());
    activationDisposers.forEach((dispose) => dispose());
  };
}
