import type { ToolCallInfo, UiMessage } from "../types";
import { formatDuration } from "./formatDuration";

function parseToolArgs(args?: string): Record<string, unknown> {
  if (!args) return {};
  try {
    const value = JSON.parse(args);
    return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
  } catch {
    return {};
  }
}

function basename(path: string): string {
  const clean = path.trim().replace(/\\/g, "/").replace(/\/+$/, "");
  return clean.split("/").filter(Boolean).pop() || clean || path;
}

function stringArg(tool: ToolCallInfo, keys: string[]): string {
  const parsed = parseToolArgs(tool.args);
  for (const key of keys) {
    const value = parsed[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

export function toolDisplayTitle(tool: ToolCallInfo): string {
  if (tool.uiLabel?.trim()) return tool.uiLabel.trim();
  const name = String(tool.name || "").trim();
  const lower = name.toLowerCase();
  const path = stringArg(tool, ["path", "file_path"]);
  const command = stringArg(tool, ["command"]);
  if (["write", "file_write"].includes(lower)) return path ? `写入 ${basename(path)}` : "写入文件";
  if (["edit", "patch", "apply_patch"].includes(lower)) return path ? `修改 ${basename(path)}` : "修改文件";
  if (["read", "file_read"].includes(lower)) return path ? `读取 ${basename(path)}` : "读取文件";
  if (["bash", "terminal", "process"].includes(lower)) return command ? `运行 ${command}` : "运行命令";
  if (["grep", "search_files", "glob"].includes(lower)) {
    const query = stringArg(tool, ["query", "pattern"]);
    return query ? `搜索 ${query}` : "搜索文件";
  }
  return name || "工具调用";
}

export function formatClock(ms?: number): string {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return "";
  return new Date(ms).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function formatProcessStartTime(
  message: Pick<UiMessage, "turnStartedAt" | "timestamp">,
): string {
  return formatClock(message.turnStartedAt ?? message.timestamp);
}

export function processTimingLabel(
  message: Pick<UiMessage, "turnStartedAt" | "turnDurationMs" | "timestamp">,
  durationMs: number,
): string {
  const parts = [formatDuration(durationMs)];
  const start = formatProcessStartTime(message);
  if (start) parts.push(start);
  return parts.join(" · ");
}

export function processSummaryLabel(options: {
  isStreaming: boolean;
  toolCount: number;
  commandCount: number;
  hasThinking: boolean;
}): string {
  const { isStreaming, toolCount, commandCount, hasThinking } = options;
  if (commandCount > 0 && commandCount === toolCount) return `运行 ${commandCount} 个命令`;
  if (toolCount > 0) return `使用了 ${toolCount} 个工具`;
  if (hasThinking) return isStreaming ? "思考中" : "思考已完成";
  return "处理过程";
}
