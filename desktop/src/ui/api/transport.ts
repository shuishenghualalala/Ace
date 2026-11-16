/**
 * 共享传输层：gateway 后端（FastAPI /api/...）的 HTTP/IPC/Bridge 原语。
 *
 * - 默认 base URL 读取 localStorage 键 `Crew.gatewayBase`，
 *   缺省回落到 `http://127.0.0.1:8000`。
 * - 桌面端通过 `window.Crew.gatewayFetch` 走 Electron 主进程 IPC，
 *   避免浏览器 CORS / 端口限制；web 端直接走 fetch。
 * - 错误处理：识别 HTML 响应（典型为 SPA fallback）和 JSON 错误体（提取 error / detail），
 *   把 HTTP 状态码 + 后端错误信息合成人类可读消息抛给 UI 层。
 */
import { logStream } from '../stream-debug';
// BackendNotification 在 feature-work 中定义；此处仅用于 ChatChunk 类型引用。
import type { BackendNotification } from './feature-work';

export type Mode = 'agent' | 'team' | 'dynamic_kanban';
export type ChunkKind =
  | 'delta'
  | 'tool'
  | 'task'
  | 'status'
  | 'final'
  | 'error'
  | 'thinking'
  | 'plan_review'
  | 'followup_question'
  | 'todo_updated'
  | 'file_changes'
  | 'session_title'
  | 'channel_session_updated'
  | 'cron_session_created'
  | 'cron_session_updated'
  | 'audit_updated'
  | 'notification'
  | 'work_event'
  | 'feature_event'
  | 'ping'
  | 'pong';

export interface Attachment {
  id: string;
  name: string;
  path: string;
  type: 'image' | 'file';
  size?: number;
}

export interface SendPayload {
  query: string;
  session_id: string;
  request_id?: string;
  mode: Mode;
  workspace_id: string;
  attachments?: Attachment[];
  sub_scenario?: string;
  client_intent?: 'revision';
  /** Wiki 模式：本轮携带的 KB 与联网搜索开关（对齐 web useChat 的字段名）。 */
  wiki_kb_id?: string;
  web_search_enabled?: boolean;
  /** Work 模式：仅本轮不应用的偏好 ID。 */
  work_disabled_preference_ids?: string[];
}

export interface ChatChunk {
  kind: ChunkKind | 'security_approval';
  body: Record<string, unknown>;
  is_final: boolean;
  sequence: number;
  request_id?: string;
  session_id?: string;
  /** Gateway 侧单调序号，用于断线 replay 与客户端去重。 */
  gateway_sequence?: number;
  /** kind === 'notification' 时携带的通知对象（owner 级广播帧）。 */
  notification?: BackendNotification;
}

/** gateway:upload IPC 单个文件的结果（与 gateway:fetch 返回 shape 一致）。 */
export interface GatewayUploadFileResult {
  path: string;
  ok: boolean;
  status: number;
  statusText: string;
  body: string;
  headers: Record<string, string>;
}

export interface GatewayUploadResult {
  results: GatewayUploadFileResult[];
}

export type BackendSocketStatusMeta = {
  /** 主进程为换 socket 主动 close(1000, reconnect)，非真实断连。 */
  transient?: boolean;
};

const DEFAULT_GATEWAY = 'http://127.0.0.1:8000';

export type CrewBridge = {
  getLaunchMode?: () => Promise<{ isDevLaunch: boolean; mode: 'dev' | 'account' }>;
  saveTracingExport?: (exportId: string) => Promise<{ ok: boolean; canceled?: boolean; path?: string }>;
  gatewayFetch?: typeof gatewayFetchBridge;
  gatewayStreamStart?: (
    requestId: string,
    url: string,
    init?: { method?: string; headers?: Record<string, string>; body?: string },
  ) => Promise<{ ok?: boolean }>;
  gatewayStreamCancel?: (requestId: string) => Promise<{ ok?: boolean }>;
  onGatewayStreamEvent?: (cb: (event: {
    request_id: string;
    type: 'head' | 'chunk' | 'end' | 'error';
    status?: number;
    headers?: Record<string, string>;
    text?: string;
    error?: string;
  }) => void) => () => void;
  gatewayUpload?: (url: string, files: string[]) => Promise<GatewayUploadResult>;
  gatewayWsConnect?: () => Promise<{ ok?: boolean; status?: number; error?: string }>;
  gatewayWsSend?: (payload: unknown) => Promise<{ ok?: boolean; error?: string }>;
  gatewayWsClose?: () => Promise<{ ok?: boolean }>;
  onGatewayWsEvent?: (cb: (event: unknown) => void) => () => void;
};

export function CrewBridge(): CrewBridge | undefined {
  return (window as Window & { Crew?: CrewBridge }).Crew;
}

export function gatewayBase(): string {
  return localStorage.getItem('Crew.gatewayBase') || DEFAULT_GATEWAY;
}

export function wsBase(): string {
  return gatewayBase().replace(/^http:/, 'ws:').replace(/^https:/, 'wss:');
}

export async function gatewayFetch(path: string, opts?: RequestInit): Promise<Response> {
  const url = `${gatewayBase()}${path}`;
  const bridge = CrewBridge()?.gatewayFetch;
  if (typeof bridge === 'function') {
    const pending = gatewayFetchBridge(url, opts);
    if (!opts?.signal) return pending;
    if (opts.signal.aborted) throw new DOMException('The operation was aborted.', 'AbortError');
    return Promise.race([
      pending,
      new Promise<Response>((_, reject) => {
        opts.signal?.addEventListener(
          'abort',
          () => reject(new DOMException('The operation was aborted.', 'AbortError')),
          { once: true },
        );
      }),
    ]);
  }
  return fetch(url, opts);
}

export async function gatewayFetchBridge(url: string, opts?: RequestInit): Promise<Response> {
  const headers: Record<string, string> = {};
  if (opts?.headers) {
    const h = opts.headers as Record<string, string>;
    Object.assign(headers, h);
  }
  const body = typeof opts?.body === 'string' ? opts.body : undefined;
  const initArg: { method?: string; headers: Record<string, string>; body?: string } = { headers };
  if (opts?.method !== undefined) initArg.method = opts.method;
  if (body !== undefined) initArg.body = body;
  const result = await window.Crew.gatewayFetch(url, initArg);
  return new Response(result.body, {
    status: result.status,
    statusText: result.statusText,
    headers: result.headers,
  });
}

/** 统一的 JSON 响应解析：错误体提取 error/message，HTML 响应识别为 SPA fallback。 */
export async function readJsonResponse<T>(res: Response, path: string): Promise<T> {
  const text = await res.text();
  if (!res.ok) {
    let message = `${res.status} ${path}`;
    try {
      const body = text ? JSON.parse(text) : null;
      const msg = body?.error || body?.message;
      if (msg) message = String(msg);
    } catch {
      // 非 JSON 错误体保留 HTTP 状态即可，避免把 HTML 泄露到界面。
    }
    throw new Error(message);
  }
  const contentType = res.headers.get('content-type') || '';
  if (!contentType.toLowerCase().includes('application/json')) {
    const preview = text.trim().slice(0, 32).toLowerCase();
    if (preview.startsWith('<!doctype') || preview.startsWith('<html')) {
      throw new Error(`当前服务未提供接口 ${path}，请重启后端服务后重试。`);
    }
    throw new Error(`接口 ${path} 返回了无法识别的数据。`);
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(`接口 ${path} 返回的数据格式异常。`);
  }
}

export async function getJSON<T>(path: string, opts?: RequestInit): Promise<T> {
  return readJsonResponse<T>(await gatewayFetch(path, opts), path);
}

/**
 * 桌面端本地文件上传：走主进程 gateway:upload IPC
 * （gateway:fetch 桥不透传二进制 body）。一次一个文件，与后端逐文件接收对齐。
 */
export async function uploadJSON<T>(path: string, filePath: string): Promise<T> {
  const bridge = CrewBridge()?.gatewayUpload;
  if (typeof bridge !== 'function') {
    throw new Error('当前环境不支持本地文件上传（需要桌面端）。');
  }
  const result = await bridge(`${gatewayBase()}${path}`, [filePath]);
  const item = result?.results?.[0];
  if (!item) {
    throw new Error('上传失败：主进程未返回结果。');
  }
  const res = new Response(item.body, {
    // Response 构造只接受 200-599；本地失败合成体均在此范围，这里兜底钳制。
    status: item.status >= 200 && item.status <= 599 ? item.status : 500,
    statusText: item.statusText,
    headers: item.headers,
  });
  return readJsonResponse<T>(res, path);
}

export const jsonBody = (body: object): RequestInit => ({
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

/** 拼接 wiki 接口的 kb_id query 参数（无 kbId 时后端回落 default KB）。 */
export function withKb(path: string, kbId?: string): string {
  return kbId ? `${path}${path.includes('?') ? '&' : '?'}kb_id=${encodeURIComponent(kbId)}` : path;
}

/** 解析 Dynamic Kanban resume 的 SSE 流，产出 ChatChunk。 */
export async function* readDynamicKanbanResumeStream(sessionId: string): AsyncGenerator<ChatChunk> {
  // gatewayFetch 内部会拼接 gatewayBase()，这里只传 path（传完整 URL 会拼成双重 base，
  // 导致桌面端 IPC 校验报 "url: not a valid URL"）。
  const path = `/api/dynamic-kanban/${encodeURIComponent(sessionId)}/resume`;
  const res = await gatewayFetch(path, { method: 'POST' });
  if (!res.ok) {
    let message = `恢复 workflow 失败: ${res.status}`;
    try {
      const body = await res.text();
      const parsed = body ? JSON.parse(body) : null;
      if (parsed?.error || parsed?.message) message = String(parsed.error || parsed.message);
    } catch {
      // ignore
    }
    throw new Error(message);
  }

  // 桌面端 IPC 可能直接返回完整文本 body，ReadableStream 不一定可用
  const text = await res.text().catch(() => '');
  const lines = text.split('\n');
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed.startsWith('data: ')) continue;
    const data = trimmed.slice(6).trim();
    if (data === '[DONE]') return;
    if (!data) continue;
    try {
      const parsed = JSON.parse(data) as ChatChunk;
      if (parsed.request_id && !parsed.session_id) {
        parsed.session_id = sessionId;
      }
      yield parsed;
    } catch {
      // 忽略无法解析的行
    }
  }
}
