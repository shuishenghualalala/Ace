import type { SubagentApiRecord, SubagentListResponse, UiMessage } from "../types";

export type SubagentActivityStatus =
  | "running"
  | "ready"
  | "idle"
  | "completed"
  | "error"
  | "timed_out"
  | "cancelled";

export interface SubagentActivity {
  id: string;
  label: string;
  provider?: string;
  lifecycle?: string;
  status: SubagentActivityStatus;
  stopReason?: string;
  timestamp?: number;
}

const TERMINAL_STATUS: Record<string, SubagentActivityStatus> = {
  aborted: "cancelled",
  cancelled: "cancelled",
  error: "error",
  failed: "error",
  timed_out: "timed_out",
  timeout: "timed_out",
};

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function number(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function recordId(record: SubagentApiRecord): string {
  return text(record.subagent_id) || text(record.child_id);
}

export function normalizeSubagentItems(
  items: SubagentListResponse["items"] | undefined,
): SubagentApiRecord[] {
  if (Array.isArray(items)) return items;
  if (!items || typeof items !== "object") return [];
  return Object.values(items).flatMap((value) => (Array.isArray(value) ? value : []));
}

export function statusFromSubagentRecord(
  record: SubagentApiRecord,
  active = false,
): SubagentActivityStatus {
  if (active) return "running";
  const explicit = text(record.state).toLowerCase() || text(record.status).toLowerCase();
  if (explicit === "running") return "running";
  if (explicit === "ready" || explicit === "idle") return explicit;
  const stopReason = text(record.stop_reason).toLowerCase();
  if (stopReason && TERMINAL_STATUS[stopReason]) return TERMINAL_STATUS[stopReason];
  if (explicit === "settled" || explicit === "completed" || explicit === "done") return "completed";
  return "idle";
}

function activityFromRecord(record: SubagentApiRecord, active = false): SubagentActivity | null {
  const id = recordId(record);
  if (!id) return null;
  const stopReason = text(record.stop_reason) || undefined;
  return {
    id,
    label: text(record.label) || "子智能体",
    provider: text(record.provider) || undefined,
    lifecycle: text(record.lifecycle) || undefined,
    status: statusFromSubagentRecord(record, active),
    stopReason,
    timestamp: number(record.settled_at) ?? number(record.created_at) ?? number(record.started_at),
  };
}

function statusFromLifecycleMessage(
  message: UiMessage,
  current: SubagentActivity | undefined,
): SubagentActivityStatus | null {
  if (message.subagentState === "started") return "running";
  if (message.subagentState === "running") return "running";
  if (message.subagentState === "ready") return "ready";
  if (message.subagentState === "idle") return "idle";
  if (message.subagentState !== "settled") return null;
  const reason = text(message.subagentStopReason).toLowerCase();
  return TERMINAL_STATUS[reason] || (current?.status === "error" ? "error" : "completed");
}

export function mergeSubagentActivities(
  response: SubagentListResponse | null | undefined,
  messages: UiMessage[] = [],
): SubagentActivity[] {
  const byId = new Map<string, SubagentActivity>();

  for (const record of response?.runs || []) {
    const activity = activityFromRecord(record);
    if (activity) byId.set(activity.id, activity);
  }
  for (const record of normalizeSubagentItems(response?.items)) {
    const activity = activityFromRecord(record, true);
    if (!activity) continue;
    const previous = byId.get(activity.id);
    byId.set(activity.id, {
      ...previous,
      ...activity,
      timestamp: activity.timestamp ?? previous?.timestamp,
    });
  }

  for (const message of messages) {
    const id = text(message.subagentId);
    if (!id) continue;
    const current = byId.get(id);
    const status = statusFromLifecycleMessage(message, current);
    if (!status) continue;
    byId.set(id, {
      id,
      label: text(message.subagentLabel) || current?.label || "子智能体",
      provider: text(message.subagentProvider) || current?.provider,
      lifecycle: current?.lifecycle,
      status,
      stopReason: text(message.subagentStopReason) || current?.stopReason,
      timestamp: message.timestamp ?? current?.timestamp,
    });
  }

  const weight = (status: SubagentActivityStatus): number => {
    if (status === "running") return 0;
    if (status === "ready") return 1;
    if (status === "idle") return 2;
    return 3;
  };
  return [...byId.values()].sort((a, b) => {
    const statusDelta = weight(a.status) - weight(b.status);
    if (statusDelta !== 0) return statusDelta;
    return (b.timestamp ?? 0) - (a.timestamp ?? 0);
  });
}

export function subagentStatusLabel(status: SubagentActivityStatus): string {
  switch (status) {
    case "running": return "运行中";
    case "ready": return "待继续";
    case "idle": return "空闲";
    case "error": return "失败";
    case "timed_out": return "超时";
    case "cancelled": return "已取消";
    default: return "已完成";
  }
}
