import type { ToolCallInfo } from "../types";
import { backendDurationToMs } from "./backendTime";

/** 把 team/delta 等帧中的文本字段归一为字符串。 */
export function normalizeTeamText(value: unknown): string {
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

/** 把 Chunk 中的 tool_calls 归一化为 ToolCallInfo。 */
export function normalizeChunkToolCalls(raw: unknown): ToolCallInfo[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const calls = raw.map((item, index) => {
    const value = item && typeof item === "object" ? (item as Record<string, unknown>) : {};
    return {
      toolCallId: String(value.id || value.tool_call_id || `team_tool_${index}`),
      name: String(value.name || "unknown"),
      uiLabel: typeof value.ui_label === "string" ? value.ui_label : undefined,
      args:
        typeof value.arguments === "string"
          ? value.arguments
          : JSON.stringify(value.arguments || {}),
      result: typeof value.result === "string" ? value.result : "",
      status: value.status === "running" || value.status === "error" ? value.status : "done",
      startedAt: typeof value.started_at === "number" ? value.started_at * 1000 : 0,
      duration:
        typeof value.duration === "number"
          ? backendDurationToMs(value.duration) || undefined
          : undefined,
    } satisfies ToolCallInfo;
  });
  return calls.length > 0 ? calls : undefined;
}
