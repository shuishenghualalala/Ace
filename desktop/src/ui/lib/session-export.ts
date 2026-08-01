/**
 * 单会话导出：拉取 /api/session/{id} 消息历史，下载 JSON。
 * 将会话数据整理为可下载的 JSON 文件。
 */

import { backendApi } from '../backend-client';
import { notify } from '../state';

function sanitizeFilenamePart(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._\u4e00-\u9fff-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48);
}

function sessionExportFilename(sessionId: string, title?: string | null): string {
  const titlePart = title ? sanitizeFilenamePart(title) : '';
  const idPart = sanitizeFilenamePart(sessionId).slice(0, 8) || 'session';
  return `${titlePart || 'session'}-${idPart}.json`;
}

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

/** 导出单个会话的完整消息历史为 JSON 文件。 */
export async function exportSessionToJson(sessionId: string, title?: string | null): Promise<void> {
  if (!sessionId) return;
  try {
    const messages = await backendApi.history(sessionId);
    const payload = {
      exported_at: new Date().toISOString(),
      session_id: sessionId,
      title: title ?? null,
      message_count: messages.length,
      messages,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    triggerDownload(blob, sessionExportFilename(sessionId, title));
    notify('会话已导出');
  } catch (err) {
    notify(`导出失败：${(err as Error).message || '未知错误'}`);
  }
}

/** 批量导出多个会话（含消息正文）；用于设置页「导出全部」增强。 */
export async function exportSessionsWithMessages(
  sessions: Array<{ session_id: string; title?: string }>,
  onProgress?: (done: number, total: number) => void,
): Promise<void> {
  const results: Array<{
    session_id: string;
    title: string | null;
    message_count: number;
    messages: unknown[];
    error?: string;
  }> = [];
  let done = 0;
  for (const s of sessions) {
    try {
      const messages = await backendApi.history(s.session_id);
      results.push({
        session_id: s.session_id,
        title: s.title ?? null,
        message_count: messages.length,
        messages,
      });
    } catch (err) {
      results.push({
        session_id: s.session_id,
        title: s.title ?? null,
        message_count: 0,
        messages: [],
        error: (err as Error).message || '拉取失败',
      });
    }
    done += 1;
    onProgress?.(done, sessions.length);
  }
  const payload = {
    exported_at: new Date().toISOString(),
    session_count: sessions.length,
    sessions: results,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  triggerDownload(blob, `Crew-sessions-full-${new Date().toISOString().slice(0, 10)}.json`);
  notify(`已导出 ${sessions.length} 个会话（含消息）`);
}
