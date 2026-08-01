const UI_TOOL_RESULT_MAX = 1200;

function clip(text: string, maxLen: number): string {
  if (maxLen <= 0) return "";
  return text.length <= maxLen ? text : text.slice(0, maxLen);
}

function terminalDetail(content: string, maxLen: number): string {
  try {
    const data = JSON.parse(content) as Record<string, unknown>;
    const output = typeof data.output === "string" ? data.output.trim() : "";
    if (output) {
      return output.length > maxLen ? output.slice(-maxLen) : output;
    }
    if (typeof data.session_id === "string" && data.session_id) {
      return `Background process started: ${data.session_id}`;
    }
    const err = typeof data.error === "string" ? data.error.trim() : "";
    if (err) return clip(err, maxLen);
  } catch {
    // plain text fallback
  }
  return clip(content, maxLen);
}

export function formatToolResultDisplay(toolName: string, raw?: string | null): string {
  const text = (raw ?? "").trim();
  if (!text) return "";

  if (toolName === "terminal") {
    return terminalDetail(text, UI_TOOL_RESULT_MAX);
  }

  try {
    const data = JSON.parse(text) as Record<string, unknown>;
    if (data && typeof data === "object") {
      for (const key of ["output", "content", "text", "result"] as const) {
        const value = data[key];
        if (typeof value === "string" && value.trim()) {
          return clip(value.trim(), UI_TOOL_RESULT_MAX);
        }
      }
      const err = data.error;
      if (typeof err === "string" && err.trim()) {
        return clip(err.trim(), UI_TOOL_RESULT_MAX);
      }
    }
  } catch {
    // plain text
  }

  return clip(text, UI_TOOL_RESULT_MAX);
}

function prettyJson(value: string): string {
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

export function formatToolResultBlock(toolName: string, raw?: string | null): string {
  const display = formatToolResultDisplay(toolName, raw);
  if (display) return display;
  return prettyJson(raw ?? "");
}
