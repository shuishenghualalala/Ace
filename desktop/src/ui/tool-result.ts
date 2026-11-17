/**
 * 工具结果展示：从结构化 JSON（尤其 terminal）提取用户可读的 stdout。
 * 与后端 tool_result_display.py 语义对齐，用于历史回放等仍带原始 JSON 的场景。
 */

const UI_TOOL_RESULT_MAX = 1200;

function clip(text: string, maxLen: number): string {
  if (maxLen <= 0) return '';
  return text.length <= maxLen ? text : text.slice(0, maxLen);
}

function terminalDetail(content: string, maxLen: number): string {
  try {
    const data = JSON.parse(content) as Record<string, unknown>;
    const status = data.status;
    const sessionId = typeof data.session_id === 'string' ? data.session_id : '';
    const legacyBackground = status == null && data.background == null && data.exit_code == null && Boolean(sessionId);
    if (status === 'backgrounded' || data.background === true || legacyBackground) {
      return clip(sessionId ? `Background process started: ${sessionId}` : 'Background process started', maxLen);
    }
    const output = typeof data.output === 'string' ? data.output.trim() : '';
    let error = typeof data.error === 'string' ? data.error.trim() : '';
    if (!error) {
      if (status === 'timed_out') error = 'terminal timed out';
      else if (status === 'cancelled') error = 'terminal cancelled';
      else if (typeof data.exit_code === 'number' && data.exit_code !== 0) {
        error = `terminal exited with code ${data.exit_code}`;
      }
    }
    if (error) {
      const tailBudget = maxLen - error.length - 2;
      return output && tailBudget > 0 ? `${error}\n\n${output.slice(-tailBudget)}` : clip(error, maxLen);
    }
    if (output) return output.slice(-maxLen);
    if (status === 'completed' || data.exit_code === 0) return 'terminal completed';
  } catch {
    // fall through
  }
  return clip(content, maxLen);
}

/** 把工具 result 字符串格式化为对话页「结果」区应展示的文本。 */
export function formatToolResultDisplay(toolName: string, raw?: string | null): string {
  const text = (raw ?? '').trim();
  if (!text) return '';

  if (toolName === 'terminal') {
    return terminalDetail(text, UI_TOOL_RESULT_MAX);
  }

  try {
    const data = JSON.parse(text) as Record<string, unknown>;
    if (data && typeof data === 'object') {
      for (const key of ['output', 'content', 'text', 'result'] as const) {
        const value = data[key];
        if (typeof value === 'string' && value.trim()) {
          return clip(value.trim(), UI_TOOL_RESULT_MAX);
        }
      }
      const err = data.error;
      if (typeof err === 'string' && err.trim()) {
        return clip(err.trim(), UI_TOOL_RESULT_MAX);
      }
    }
  } catch {
    // plain text
  }

  return clip(text, UI_TOOL_RESULT_MAX);
}
