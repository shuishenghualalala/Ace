/**
 * IPC 参数 schema 集中校验（手写最小 validator，零依赖）。
 *
 * 设计要点：
 * - 每个 schema 暴露 `parse(args: unknown): { ok: true; value: T } | { ok: false; error: string }`
 * - 不依赖 zod / ajv 等第三方库，保持 Electron 主进程 bundle 体积
 * - 校验失败抛出的 Error.message 必须包含可定位的字段名，便于日志/UI 排查
 *
 * 使用示例（主进程）：
 *   import { GatewayFetchArgs } from '../shared/ipc-schemas';
 *   ipcMain.handle('gateway:fetch', (_e, raw) => {
 *     const args = GatewayFetchArgs.parse(raw);
 *     if (!args.ok) throw new Error(`IPC_ARG_VALIDATION_FAILED: ${args.error}`);
 *     ...
 *   });
 */

import {
  GATEWAY_FETCH_ALLOWED_HOSTNAMES,
  GATEWAY_FETCH_ALLOWED_PATH_PREFIXES,
  GATEWAY_UPLOAD_ALLOWED_PATHS,
  GATEWAY_UPLOAD_MAX_FILES,
  MAX_DIALOG_FILE_BYTES,
  OPEN_EXTERNAL_ALLOWED_PROTOCOLS,
} from './constants';

// ---------- 类型工具 ----------

export type ParseResult<T> =
  | { ok: true; value: T }
  | { ok: false; error: string };

function fail(field: string, reason: string): ParseResult<never> {
  return { ok: false, error: `${field}: ${reason}` };
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

const MAX_IPC_PATH_CHARS = 4096;
const MAX_SECURITY_IDENTIFIER_CHARS = 200;
const MAX_SECURITY_ARGV_TOKENS = 128;
const MAX_SECURITY_ARGV_TOKEN_CHARS = 4096;
const MAX_SECURITY_PERMISSIONS_BYTES = 32 * 1024;
const MAX_GATEWAY_URL_CHARS = 4096;
const MAX_GATEWAY_BODY_BYTES = 8 * 1024 * 1024;
const MAX_GATEWAY_HEADERS = 64;
const MAX_GATEWAY_HEADER_NAME_CHARS = 128;
const MAX_GATEWAY_HEADER_VALUE_BYTES = 8 * 1024;
const MAX_GATEWAY_HEADERS_BYTES = 64 * 1024;
const MAX_FILE_WRITE_BYTES = MAX_DIALOG_FILE_BYTES;
const MAX_FILE_WRITE_BASE64_CHARS = Math.ceil(MAX_FILE_WRITE_BYTES / 3) * 4;
const HTTP_METHODS = new Set(['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD']);

function hasControlCharacters(value: string): boolean {
  for (const character of value) {
    const code = character.charCodeAt(0);
    if (code <= 0x1f || code === 0x7f) return true;
  }
  return false;
}

function utf8Length(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function parseIpcPath(raw: unknown, field = 'path'): ParseResult<string> {
  const parsed = StringSchema.parse(raw, field);
  if (!parsed.ok) return parsed;
  if (parsed.value.length > MAX_IPC_PATH_CHARS) {
    return fail(field, `max ${MAX_IPC_PATH_CHARS} chars`);
  }
  if (parsed.value.includes('\0')) return fail(field, 'must not contain NUL');
  return parsed;
}

function parseSecurityIdentifier(raw: unknown, field: string): ParseResult<string> {
  const parsed = StringSchema.parse(raw, field);
  if (!parsed.ok) return parsed;
  if (parsed.value.length > MAX_SECURITY_IDENTIFIER_CHARS) {
    return fail(field, `max ${MAX_SECURITY_IDENTIFIER_CHARS} chars`);
  }
  if (hasControlCharacters(parsed.value)) {
    return fail(field, 'must not contain control characters');
  }
  return parsed;
}

function parseSecureUpdateUrl(raw: unknown, field = 'url'): ParseResult<string> {
  const parsed = StringSchema.parse(raw, field);
  if (!parsed.ok) return parsed;
  if (parsed.value.length > MAX_GATEWAY_URL_CHARS) {
    return fail(field, `max ${MAX_GATEWAY_URL_CHARS} chars`);
  }
  let url: URL;
  try {
    url = new URL(parsed.value);
  } catch {
    return fail(field, 'not a valid URL');
  }
  if (
    url.protocol !== 'https:'
    || url.username !== ''
    || url.password !== ''
    || url.hash !== ''
  ) {
    return fail(field, 'must be a credential-free HTTPS URL without a fragment');
  }
  return parsed;
}

// ---------- 基础原子 validator ----------

export const StringSchema = {
  parse(v: unknown, field = 'value'): ParseResult<string> {
    if (typeof v !== 'string') return fail(field, `expected string, got ${typeof v}`);
    if (v.length === 0) return fail(field, 'must be non-empty');
    return { ok: true, value: v };
  },
};

export const OptionalStringSchema = {
  parse(v: unknown, field = 'value'): ParseResult<string | undefined> {
    if (v === undefined) return { ok: true, value: undefined };
    if (typeof v !== 'string') return fail(field, `expected string|undefined, got ${typeof v}`);
    return { ok: true, value: v };
  },
};

export const BooleanSchema = {
  parse(v: unknown, field = 'value'): ParseResult<boolean> {
    if (typeof v !== 'boolean') return fail(field, `expected boolean, got ${typeof v}`);
    return { ok: true, value: v };
  },
};

export const NumberSchema = {
  parse(v: unknown, field = 'value'): ParseResult<number> {
    if (typeof v !== 'number' || !Number.isFinite(v)) {
      return fail(field, `expected finite number, got ${typeof v}`);
    }
    return { ok: true, value: v };
  },
};

// ---------- 业务 schema ----------

/**
 * gateway:fetch args.
 * 限定 path 必须以白名单前缀开头，hostname 由主进程强制重写为本地 gateway，
 * 因此 path 才是真正的安全边界。
 */
export interface GatewayFetchArgs {
  url: string;
  init?: { method?: string; headers?: Record<string, string>; body?: string };
}

export const GatewayFetchArgs = {
  parse(raw: unknown): ParseResult<GatewayFetchArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const urlResult = StringSchema.parse(raw['url'], 'url');
    if (!urlResult.ok) return urlResult;
    if (urlResult.value.length > MAX_GATEWAY_URL_CHARS) {
      return fail('url', `max ${MAX_GATEWAY_URL_CHARS} chars`);
    }
    let parsedUrl: URL;
    try {
      parsedUrl = new URL(urlResult.value);
    } catch {
      return fail('url', 'not a valid URL');
    }
    const hostname = parsedUrl.hostname;
    if (
      parsedUrl.protocol !== 'http:'
      || parsedUrl.username !== ''
      || parsedUrl.password !== ''
      || parsedUrl.hash !== ''
    ) {
      return fail('url', 'must be a credential-free local HTTP URL without a fragment');
    }
    if (!GATEWAY_FETCH_ALLOWED_HOSTNAMES.has(hostname)) {
      return fail('url.hostname', `must be one of ${Array.from(GATEWAY_FETCH_ALLOWED_HOSTNAMES).join(', ')}`);
    }
    if (!GATEWAY_FETCH_ALLOWED_PATH_PREFIXES.some((p) => parsedUrl.pathname.startsWith(p))) {
      return fail('url.pathname', `must start with one of ${GATEWAY_FETCH_ALLOWED_PATH_PREFIXES.join(', ')}`);
    }

    const initRaw = raw['init'];
    let init: GatewayFetchArgs['init'];
    if (initRaw !== undefined) {
      if (!isPlainObject(initRaw)) return fail('init', 'expected object');
      const method = OptionalStringSchema.parse(initRaw['method'], 'init.method');
      if (!method.ok) return method;
      const body = OptionalStringSchema.parse(initRaw['body'], 'init.body');
      if (!body.ok) return body;
      const normalizedMethod = method.value?.toUpperCase();
      if (normalizedMethod !== undefined && !HTTP_METHODS.has(normalizedMethod)) {
        return fail('init.method', 'unexpected HTTP method');
      }
      if (body.value !== undefined && utf8Length(body.value) > MAX_GATEWAY_BODY_BYTES) {
        return fail('init.body', `max ${MAX_GATEWAY_BODY_BYTES} UTF-8 bytes`);
      }
      const headersRaw = initRaw['headers'];
      let headers: Record<string, string> | undefined;
      if (headersRaw !== undefined) {
        if (!isPlainObject(headersRaw)) return fail('init.headers', 'expected object');
        const entries = Object.entries(headersRaw);
        if (entries.length > MAX_GATEWAY_HEADERS) {
          return fail('init.headers', `max ${MAX_GATEWAY_HEADERS} entries`);
        }
        headers = {};
        let headerBytes = 0;
        for (const [k, v] of entries) {
          if (typeof v !== 'string') return fail(`init.headers.${k}`, 'expected string');
          if (
            k.length === 0
            || k.length > MAX_GATEWAY_HEADER_NAME_CHARS
            || hasControlCharacters(k)
          ) {
            return fail('init.headers', 'invalid header name');
          }
          const valueBytes = utf8Length(v);
          if (valueBytes > MAX_GATEWAY_HEADER_VALUE_BYTES || hasControlCharacters(v)) {
            return fail(`init.headers.${k}`, 'header value is invalid or too large');
          }
          headerBytes += utf8Length(k) + valueBytes;
          if (headerBytes > MAX_GATEWAY_HEADERS_BYTES) {
            return fail('init.headers', `max ${MAX_GATEWAY_HEADERS_BYTES} UTF-8 bytes`);
          }
          headers[k] = v;
        }
      }
      const initObj: { method?: string; headers?: Record<string, string>; body?: string } = {};
      if (normalizedMethod !== undefined) initObj.method = normalizedMethod;
      if (body.value !== undefined) initObj.body = body.value;
      if (headers) initObj.headers = headers;
      init = initObj;
    }

    const value: GatewayFetchArgs = { url: urlResult.value };
    if (init !== undefined) value.init = init;
    return { ok: true, value };
  },
};

export type SecurityApprovalDecision = 'once' | 'session' | 'always' | 'reject';

export interface SecurityPendingArgs {
  workspaceId: string;
  sessionId: string;
  taskId?: string;
}

export const SecurityPendingArgs = {
  parse(raw: unknown): ParseResult<SecurityPendingArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const workspaceId = parseSecurityIdentifier(raw['workspaceId'], 'workspaceId');
    if (!workspaceId.ok) return workspaceId;
    const sessionId = parseSecurityIdentifier(raw['sessionId'], 'sessionId');
    if (!sessionId.ok) return sessionId;
    const taskId = OptionalStringSchema.parse(raw['taskId'], 'taskId');
    if (!taskId.ok) return taskId;
    if (
      taskId.value !== undefined
      && (
        taskId.value.length > MAX_SECURITY_IDENTIFIER_CHARS
        || hasControlCharacters(taskId.value)
      )
    ) {
      return fail('taskId', 'invalid security identifier');
    }
    return { ok: true, value: { workspaceId: workspaceId.value, sessionId: sessionId.value, ...(taskId.value ? { taskId: taskId.value } : {}) } };
  },
};

export interface SecurityModeArgs {
  workspaceId: string;
  sessionId: string;
  mode: 'read_only' | 'request_approval' | 'auto_review' | 'full_access';
}

export const SecurityModeArgs = {
  parse(raw: unknown): ParseResult<SecurityModeArgs> {
    const base = SecurityPendingArgs.parse(raw);
    if (!base.ok) return base;
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const mode = raw['mode'];
    if (!['read_only', 'request_approval', 'auto_review', 'full_access'].includes(String(mode))) {
      return fail('mode', 'must be read_only, request_approval, auto_review, or full_access');
    }
    return {
      ok: true,
      value: {
        workspaceId: base.value.workspaceId,
        sessionId: base.value.sessionId,
        mode: mode as SecurityModeArgs['mode'],
      },
    };
  },
};

export interface SecurityDecisionArgs extends SecurityPendingArgs {
  taskId: string;
  requestId: string;
  decision: SecurityApprovalDecision;
  alwaysArgvPrefix?: string[];
  permissions?: Record<string, unknown>;
}

export const SecurityDecisionArgs = {
  parse(raw: unknown): ParseResult<SecurityDecisionArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const base = SecurityPendingArgs.parse(raw);
    if (!base.ok) return base;
    if (!base.value.taskId) return fail('taskId', 'required for an approval decision');
    const requestId = parseSecurityIdentifier(raw['requestId'], 'requestId');
    if (!requestId.ok) return requestId;
    const decision = raw['decision'];
    if (!['once', 'session', 'always', 'reject'].includes(String(decision))) {
      return fail('decision', 'must be once, session, always, or reject');
    }
    let alwaysArgvPrefix: string[] | undefined;
    if (raw['alwaysArgvPrefix'] !== undefined) {
      if (!Array.isArray(raw['alwaysArgvPrefix']) || raw['alwaysArgvPrefix'].length === 0) {
        return fail('alwaysArgvPrefix', 'must be a non-empty string array');
      }
      if (raw['alwaysArgvPrefix'].length > MAX_SECURITY_ARGV_TOKENS) {
        return fail('alwaysArgvPrefix', `max ${MAX_SECURITY_ARGV_TOKENS} tokens`);
      }
      alwaysArgvPrefix = [];
      for (const [index, token] of raw['alwaysArgvPrefix'].entries()) {
        const parsed = StringSchema.parse(token, `alwaysArgvPrefix.${index}`);
        if (!parsed.ok) return parsed;
        if (
          parsed.value.length > MAX_SECURITY_ARGV_TOKEN_CHARS
          || hasControlCharacters(parsed.value)
        ) {
          return fail(
            `alwaysArgvPrefix.${index}`,
            `max ${MAX_SECURITY_ARGV_TOKEN_CHARS} chars without control characters`,
          );
        }
        alwaysArgvPrefix.push(parsed.value);
      }
    }
    let permissions: Record<string, unknown> | undefined;
    if (raw['permissions'] !== undefined) {
      if (!isPlainObject(raw['permissions'])) return fail('permissions', 'must be an object');
      let serialized: string;
      try {
        serialized = JSON.stringify(raw['permissions']);
      } catch {
        return fail('permissions', 'must be JSON serializable');
      }
      if (utf8Length(serialized) > MAX_SECURITY_PERMISSIONS_BYTES) {
        return fail('permissions', `max ${MAX_SECURITY_PERMISSIONS_BYTES} UTF-8 bytes`);
      }
      permissions = raw['permissions'] as Record<string, unknown>;
    }
    return {
      ok: true,
      value: {
        ...base.value,
        taskId: base.value.taskId,
        requestId: requestId.value,
        decision: decision as SecurityApprovalDecision,
        ...(alwaysArgvPrefix ? { alwaysArgvPrefix } : {}),
        ...(permissions ? { permissions } : {}),
      },
    };
  },
};

export interface SecurityWorkspaceArgs { workspaceId: string }
export const SecurityWorkspaceArgs = {
  parse(raw: unknown): ParseResult<SecurityWorkspaceArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const workspaceId = parseSecurityIdentifier(raw['workspaceId'], 'workspaceId');
    return workspaceId.ok ? { ok: true, value: { workspaceId: workspaceId.value } } : workspaceId;
  },
};

export interface SecurityRuleMutationArgs extends SecurityWorkspaceArgs {
  ruleId: string;
  enabled?: boolean;
}
export const SecurityRuleMutationArgs = {
  parse(raw: unknown): ParseResult<SecurityRuleMutationArgs> {
    const base = SecurityWorkspaceArgs.parse(raw);
    if (!base.ok) return base;
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const ruleId = parseSecurityIdentifier(raw['ruleId'], 'ruleId');
    if (!ruleId.ok) return ruleId;
    if (raw['enabled'] !== undefined && typeof raw['enabled'] !== 'boolean') {
      return fail('enabled', 'expected boolean');
    }
    return { ok: true, value: { ...base.value, ruleId: ruleId.value, ...(typeof raw['enabled'] === 'boolean' ? { enabled: raw['enabled'] } : {}) } };
  },
};

export interface SecurityAlertActionArgs {
  alertId: string;
}
export const SecurityAlertActionArgs = {
  parse(raw: unknown): ParseResult<SecurityAlertActionArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const alertId = parseSecurityIdentifier(raw['alertId'], 'alertId');
    if (!alertId.ok) return alertId;
    return { ok: true, value: { alertId: alertId.value } };
  },
};

export interface SecurityAuditArgs {
  offset?: number;
  limit?: number;
  actionType?: '' | 'approval_requested' | 'approval_decision' | 'exec_decision'
    | 'file_decision' | 'network_decision' | 'exec_result' | 'rule_created'
    | 'rule_disabled' | 'rule_deleted' | 'audit_purged';
  decision?: '' | 'allow' | 'deny' | 'pending' | 'ask' | 'once' | 'session'
    | 'always' | 'reject' | 'completed' | 'failed' | 'cancelled' | 'error'
    | 'enabled' | 'disabled' | 'deleted';
  sessionId?: string;
  workspaceId?: string;
  taskId?: string;
  startTime?: number;
  endTime?: number;
  sort?: 'newest' | 'oldest';
}
export const SecurityAuditArgs = {
  parse(raw: unknown): ParseResult<SecurityAuditArgs> {
    if (raw === undefined) return { ok: true, value: {} };
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const value: SecurityAuditArgs = {};
    for (const key of ['offset', 'limit'] as const) {
      const item = raw[key];
      if (item !== undefined && (!Number.isInteger(item) || Number(item) < 0)) return fail(key, 'expected non-negative integer');
      if (key === 'limit' && item !== undefined && Number(item) > 100) {
        return fail(key, 'must be <= 100');
      }
      if (key === 'offset' && item !== undefined && Number(item) > 1_000_000) {
        return fail(key, 'must be <= 1000000');
      }
      if (item !== undefined) value[key] = Number(item);
    }
    const actionType = raw['actionType'];
    const actionTypes = [
      '', 'approval_requested', 'approval_decision', 'exec_decision', 'file_decision',
      'network_decision', 'exec_result', 'rule_created', 'rule_disabled', 'rule_deleted',
      'audit_purged',
    ];
    if (actionType !== undefined && !actionTypes.includes(String(actionType))) {
      return fail('actionType', 'unexpected audit action type');
    }
    if (actionType !== undefined) {
      value.actionType = String(actionType) as NonNullable<SecurityAuditArgs['actionType']>;
    }
    const decision = raw['decision'];
    const decisions = [
      '', 'allow', 'deny', 'pending', 'ask', 'once', 'session', 'always', 'reject',
      'completed', 'failed', 'cancelled', 'error', 'enabled', 'disabled', 'deleted',
    ];
    if (decision !== undefined && !decisions.includes(String(decision))) {
      return fail('decision', 'unexpected audit decision');
    }
    if (decision !== undefined) {
      value.decision = String(decision) as NonNullable<SecurityAuditArgs['decision']>;
    }
    const sessionId = raw['sessionId'];
    if (sessionId !== undefined && sessionId !== '') {
      const parsedSession = parseSecurityIdentifier(sessionId, 'sessionId');
      if (!parsedSession.ok) return parsedSession;
      value.sessionId = parsedSession.value.trim();
    }
    for (const [input, output] of [
      ['workspaceId', 'workspaceId'],
      ['taskId', 'taskId'],
    ] as const) {
      const item = raw[input];
      if (item === undefined || item === '') continue;
      const parsed = parseSecurityIdentifier(item, input);
      if (!parsed.ok) return parsed;
      value[output] = parsed.value.trim();
    }
    for (const [input, output] of [
      ['startTime', 'startTime'],
      ['endTime', 'endTime'],
    ] as const) {
      const item = raw[input];
      if (item === undefined) continue;
      const parsed = NumberSchema.parse(item, input);
      if (!parsed.ok) return parsed;
      value[output] = parsed.value;
    }
    if (
      value.startTime !== undefined
      && value.endTime !== undefined
      && value.startTime > value.endTime
    ) {
      return fail('timeRange', 'startTime must be <= endTime');
    }
    const sort = raw['sort'];
    if (sort !== undefined && sort !== 'newest' && sort !== 'oldest') {
      return fail('sort', 'expected newest or oldest');
    }
    if (sort === 'newest' || sort === 'oldest') value.sort = sort;
    return { ok: true, value };
  },
};

export interface SecuritySetupArgs { action: 'install' | 'repair' | 'uninstall' }
export const SecuritySetupArgs = {
  parse(raw: unknown): ParseResult<SecuritySetupArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const action = raw['action'];
    if (action !== 'install' && action !== 'repair' && action !== 'uninstall') {
      return fail('action', 'expected install, repair, or uninstall');
    }
    return { ok: true, value: { action } };
  },
};

/**
 * gateway:upload args。
 * 与 gateway:fetch 相同的 hostname 白名单 + 主进程 host 钳制；
 * pathname 精确匹配 GATEWAY_UPLOAD_ALLOWED_PATHS（上传端点逐个放行，不走 /api/ 前缀）。
 * files 为本地文件绝对路径数组，主进程逐文件读取并组 multipart 上传。
 */
export interface GatewayUploadArgs {
  url: string;
  files: string[];
}

/** 本地绝对路径：POSIX `/...`、Windows 盘符 `C:\...` / `C:/...`、UNC `\\...`。 */
const ABSOLUTE_PATH_RE = /^(\/|[a-zA-Z]:[\\/]|\\\\)/;

export const GatewayUploadArgs = {
  parse(raw: unknown): ParseResult<GatewayUploadArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const urlResult = StringSchema.parse(raw['url'], 'url');
    if (!urlResult.ok) return urlResult;
    if (urlResult.value.length > MAX_GATEWAY_URL_CHARS) {
      return fail('url', `max ${MAX_GATEWAY_URL_CHARS} chars`);
    }
    let parsedUrl: URL;
    try {
      parsedUrl = new URL(urlResult.value);
    } catch {
      return fail('url', 'not a valid URL');
    }
    if (
      parsedUrl.protocol !== 'http:'
      || parsedUrl.username !== ''
      || parsedUrl.password !== ''
      || parsedUrl.hash !== ''
    ) {
      return fail('url', 'must be a credential-free local HTTP URL without a fragment');
    }
    if (!GATEWAY_FETCH_ALLOWED_HOSTNAMES.has(parsedUrl.hostname)) {
      return fail('url.hostname', `must be one of ${Array.from(GATEWAY_FETCH_ALLOWED_HOSTNAMES).join(', ')}`);
    }
    if (!GATEWAY_UPLOAD_ALLOWED_PATHS.has(parsedUrl.pathname)) {
      return fail('url.pathname', `must be one of ${Array.from(GATEWAY_UPLOAD_ALLOWED_PATHS).join(', ')}`);
    }

    const filesRaw = raw['files'];
    if (!Array.isArray(filesRaw)) return fail('files', 'expected array');
    if (filesRaw.length === 0) return fail('files', 'must be non-empty');
    if (filesRaw.length > GATEWAY_UPLOAD_MAX_FILES) {
      return fail('files', `max ${GATEWAY_UPLOAD_MAX_FILES} files per request`);
    }
    const files: string[] = [];
    for (let i = 0; i < filesRaw.length; i++) {
      const file = parseIpcPath(filesRaw[i], `files[${i}]`);
      if (!file.ok) return file;
      if (!ABSOLUTE_PATH_RE.test(file.value)) {
        return fail(`files[${i}]`, 'must be an absolute path');
      }
      files.push(file.value);
    }

    return { ok: true, value: { url: urlResult.value, files } };
  },
};

/** shell:openExternal args. */
export interface ShellOpenExternalArgs {
  url: string;
}

export const ShellOpenExternalArgs = {
  parse(raw: unknown): ParseResult<ShellOpenExternalArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const urlResult = StringSchema.parse(raw['url'], 'url');
    if (!urlResult.ok) return urlResult;
    let parsedUrl: URL;
    try {
      parsedUrl = new URL(urlResult.value);
    } catch {
      return fail('url', 'not a valid URL');
    }
    if (!OPEN_EXTERNAL_ALLOWED_PROTOCOLS.has(parsedUrl.protocol)) {
      return fail('url.protocol', `must be one of ${Array.from(OPEN_EXTERNAL_ALLOWED_PROTOCOLS).join(', ')}`);
    }
    return { ok: true, value: { url: urlResult.value } };
  },
};

/**
 * shell:openPath args（仅结构校验：非空字符串）。
 * 路径白名单（必须在 app.userData / downloads / documents 等允许根目录下）
 * 由主进程注入——因为 `app.getPath()` 只能在 main 侧调用，
 * 不能放进本 shared 文件（会破坏 main/preload/renderer 分层）。
 *
 * workspaceId: 可选的 Workspace ID。项目路径必须由主进程通过已鉴权 Gateway
 * 记录解析；renderer 不得直接扩大允许根目录。
 */
export interface ShellOpenPathArgs {
  path: string;
  workspaceId?: string;
}

export const ShellOpenPathArgs = {
  parse(raw: unknown): ParseResult<ShellOpenPathArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    if ('allowedRoot' in raw) {
      return fail('args.allowedRoot', 'renderer-provided allowed roots are not allowed');
    }
    const p = parseIpcPath(raw['path']);
    if (!p.ok) return p;
    const value: ShellOpenPathArgs = { path: p.value };
    const workspaceId = raw['workspaceId'];
    if (workspaceId !== undefined && workspaceId !== null) {
      const r = parseSecurityIdentifier(workspaceId, 'workspaceId');
      if (!r.ok) return r;
      value.workspaceId = r.value;
    }
    return { ok: true, value };
  },
};

/** Workspace directory probes identify a server-owned Workspace, never a renderer path. */
export interface WorkspaceDirectoryArgs {
  workspaceId: string;
}

export const WorkspaceDirectoryArgs = {
  parse(raw: unknown): ParseResult<WorkspaceDirectoryArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    if ('path' in raw || 'allowedRoot' in raw) {
      return fail('args.path', 'renderer-provided paths are not allowed');
    }
    const workspaceId = parseSecurityIdentifier(raw['workspaceId'], 'workspaceId');
    if (!workspaceId.ok) return workspaceId;
    return { ok: true, value: { workspaceId: workspaceId.value } };
  },
};

/** shell:openPathWith args。applicationId 必须由 shell:listOpenApplications 返回。 */
export interface ShellOpenPathWithArgs {
  path: string;
  applicationId: string;
}

export const ShellOpenPathWithArgs = {
  parse(raw: unknown): ParseResult<ShellOpenPathWithArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const pathResult = parseIpcPath(raw['path']);
    if (!pathResult.ok) return pathResult;
    const applicationId = StringSchema.parse(raw['applicationId'], 'applicationId');
    if (!applicationId.ok) return applicationId;
    return {
      ok: true,
      value: {
        path: pathResult.value,
        applicationId: applicationId.value,
      },
    };
  },
};

/**
 * wiki:openSourceFile args（仅结构校验）。
 * 渲染进程只传 sourceId/kbId，文件路径由主进程向 gateway 查询来源元数据后
 * 自行校验（必须在 CREW_HOME 内）再打开，渲染进程无法伪造任意路径。
 */
export interface WikiOpenSourceFileArgs {
  sourceId: string;
  kbId?: string;
}

export const WikiOpenSourceFileArgs = {
  parse(raw: unknown): ParseResult<WikiOpenSourceFileArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const sourceId = StringSchema.parse(raw['sourceId'], 'sourceId');
    if (!sourceId.ok) return sourceId;
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(sourceId.value)) {
      return fail('sourceId', 'invalid characters');
    }
    const value: WikiOpenSourceFileArgs = { sourceId: sourceId.value };
    const kbId = raw['kbId'];
    if (kbId !== undefined && kbId !== null) {
      const k = StringSchema.parse(kbId, 'kbId');
      if (!k.ok) return k;
      if (/[/\\:]/.test(k.value) || k.value.includes('\0') || k.value.length > 64) {
        return fail('kbId', 'invalid characters');
      }
      value.kbId = k.value;
    }
    return { ok: true, value };
  },
};

export interface ShellWriteTextFileArgs {
  path: string;
  content: string;
}

export const ShellWriteTextFileArgs = {
  parse(raw: unknown): ParseResult<ShellWriteTextFileArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const p = parseIpcPath(raw['path']);
    if (!p.ok) return p;
    const content = raw['content'];
    if (typeof content !== 'string') return fail('content', `expected string, got ${typeof content}`);
    if (utf8Length(content) > MAX_FILE_WRITE_BYTES) {
      return fail('content', `max ${MAX_FILE_WRITE_BYTES} UTF-8 bytes`);
    }
    return { ok: true, value: { path: p.value, content } };
  },
};

export interface ShellWriteFileBase64Args {
  path: string;
  base64: string;
}

export const ShellWriteFileBase64Args = {
  parse(raw: unknown): ParseResult<ShellWriteFileBase64Args> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const p = parseIpcPath(raw['path']);
    if (!p.ok) return p;
    const base64 = StringSchema.parse(raw['base64'], 'base64');
    if (!base64.ok) return base64;
    if (base64.value.length > MAX_FILE_WRITE_BASE64_CHARS) {
      return fail('base64', `max ${MAX_FILE_WRITE_BASE64_CHARS} chars`);
    }
    if (!/^[A-Za-z0-9+/]+={0,2}$/.test(base64.value)) return fail('base64', 'invalid base64');
    return { ok: true, value: { path: p.value, base64: base64.value } };
  },
};

/** update:start-download args — 客户端按 version + 当前 OS 拼下载 URL，故只需版本号。 */
export interface UpdateStartDownloadArgs {
  version: string;
  type: 'force' | 'reminder';
  url?: string | undefined;
}

export const UpdateStartDownloadArgs = {
  parse(raw: unknown): ParseResult<UpdateStartDownloadArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const version = StringSchema.parse(raw['version'], 'version');
    if (!version.ok) return version;
    const normalizedVersion = version.value.trim();
    if (
      !/^[0-9A-Za-z][0-9A-Za-z.+-]{0,127}$/.test(normalizedVersion)
      || hasControlCharacters(normalizedVersion)
    ) {
      return fail('version', 'must be a bounded release identifier');
    }
    const rawType = raw['type'];
    if (rawType !== 'force' && rawType !== 'reminder') {
      return fail('type', 'must be force or reminder');
    }
    const url = raw['url'];
    let normalizedUrl: string | undefined;
    if (url !== undefined) {
      const parsedUrl = parseSecureUpdateUrl(url);
      if (!parsedUrl.ok) return parsedUrl;
      normalizedUrl = parsedUrl.value;
    }
    return {
      ok: true,
      value: {
        version: normalizedVersion,
        type: rawType,
        ...(normalizedUrl ? { url: normalizedUrl } : {}),
      },
    };
  },
};

export interface UpdateDownloadArgs {
  url: string;
}

export const UpdateDownloadArgs = {
  parse(raw: unknown): ParseResult<UpdateDownloadArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const url = parseSecureUpdateUrl(raw['url']);
    if (!url.ok) return url;
    return { ok: true, value: { url: url.value } };
  },
};

export interface UpdateInstallArgs {
  filePath: string;
}

export const UpdateInstallArgs = {
  parse(raw: unknown): ParseResult<UpdateInstallArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const filePath = StringSchema.parse(raw['filePath'], 'filePath');
    if (!filePath.ok) return filePath;
    return { ok: true, value: { filePath: filePath.value } };
  },
};

/** Sanitized feedback draft shared by preview and authorized submit. */
export interface FeedbackPayloadArgs {
  title: string;
  description: string;
  images?: Array<{ name: string; dataUrl: string }>;
}

const MAX_FEEDBACK_IMAGES = 9;
const MAX_FEEDBACK_IMAGE_BYTES = 4 * 1024 * 1024;
const MAX_FEEDBACK_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024;
const FEEDBACK_DATA_URL_RE =
  /^data:(image\/(?:gif|jpeg|png|webp));base64,([A-Za-z0-9+/]*={0,2})$/i;

function parseFeedbackPayload(raw: unknown): ParseResult<FeedbackPayloadArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const title = StringSchema.parse(raw['title'], 'title');
    if (!title.ok) return title;
    if (title.value.length > 200) return fail('title', 'max 200 chars');
    const description = StringSchema.parse(raw['description'], 'description');
    if (!description.ok) return description;
    if (description.value.length > 5000) return fail('description', 'max 5000 chars');

    const imagesRaw = raw['images'];
    let images: FeedbackPayloadArgs['images'];
    if (imagesRaw !== undefined) {
      if (!Array.isArray(imagesRaw)) return fail('images', 'expected array');
      if (imagesRaw.length > MAX_FEEDBACK_IMAGES) {
        return fail('images', `max ${MAX_FEEDBACK_IMAGES}`);
      }
      images = [];
      let totalImageBytes = 0;
      for (let i = 0; i < imagesRaw.length; i++) {
        const item = imagesRaw[i];
        if (!isPlainObject(item)) return fail(`images[${i}]`, 'expected object');
        const name = StringSchema.parse(item['name'], `images[${i}].name`);
        if (!name.ok) return name;
        if (
          name.value.length > 128
          || /[\/\\]/.test(name.value)
          || hasControlCharacters(name.value)
          || name.value === '.'
          || name.value === '..'
        ) {
          return fail(`images[${i}].name`, 'must be a safe leaf name up to 128 chars');
        }
        const dataUrl = StringSchema.parse(item['dataUrl'], `images[${i}].dataUrl`);
        if (!dataUrl.ok) return dataUrl;
        const match = FEEDBACK_DATA_URL_RE.exec(dataUrl.value);
        if (!match?.[2]) {
          return fail(
            `images[${i}].dataUrl`,
            'must be a base64 GIF, JPEG, PNG, or WebP data URL',
          );
        }
        const base64 = match[2];
        const padding = base64.endsWith('==') ? 2 : (base64.endsWith('=') ? 1 : 0);
        const decodedBytes = Math.floor(base64.length * 3 / 4) - padding;
        if (decodedBytes <= 0 || decodedBytes > MAX_FEEDBACK_IMAGE_BYTES) {
          return fail(`images[${i}].dataUrl`, `max ${MAX_FEEDBACK_IMAGE_BYTES} decoded bytes`);
        }
        totalImageBytes += decodedBytes;
        if (totalImageBytes > MAX_FEEDBACK_TOTAL_IMAGE_BYTES) {
          return fail('images', `max ${MAX_FEEDBACK_TOTAL_IMAGE_BYTES} decoded bytes total`);
        }
        images.push({ name: name.value, dataUrl: dataUrl.value });
      }
    }

    const value: FeedbackPayloadArgs = {
      title: title.value,
      description: description.value,
    };
    if (images !== undefined) value.images = images;
    return { ok: true, value };
}

/** feedback:preview args. Previewing never persists or uploads the draft. */
export type FeedbackPreviewArgs = FeedbackPayloadArgs;
export const FeedbackPreviewArgs = {
  parse(raw: unknown): ParseResult<FeedbackPreviewArgs> {
    return parseFeedbackPayload(raw);
  },
};

/** feedback:submit args. A main-process-issued one-time authority is mandatory. */
export interface FeedbackSubmitArgs extends FeedbackPayloadArgs {
  authority: string;
}

export const FeedbackSubmitArgs = {
  parse(raw: unknown): ParseResult<FeedbackSubmitArgs> {
    const payload = parseFeedbackPayload(raw);
    if (!payload.ok) return payload;
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const authority = StringSchema.parse(raw['authority'], 'authority');
    if (!authority.ok) return authority;
    if (
      authority.value.length > 256
      || hasControlCharacters(authority.value)
      || !/^[A-Za-z0-9_-]+$/.test(authority.value)
    ) {
      return fail('authority', 'must be a bounded opaque token');
    }
    return { ok: true, value: { ...payload.value, authority: authority.value } };
  },
};

/** feedback:cancel args. Cancellation accepts only the opaque main authority. */
export interface FeedbackCancelArgs {
  authority: string;
}

export const FeedbackCancelArgs = {
  parse(raw: unknown): ParseResult<FeedbackCancelArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const authority = StringSchema.parse(raw['authority'], 'authority');
    if (!authority.ok) return authority;
    if (
      authority.value.length > 256
      || hasControlCharacters(authority.value)
      || !/^[A-Za-z0-9_-]+$/.test(authority.value)
    ) {
      return fail('authority', 'must be a bounded opaque token');
    }
    return { ok: true, value: { authority: authority.value } };
  },
};

/**
 * feedback:list args.
 * page: positive int (default 1), size: positive int capped at 100 (default 20),
 * status: optional enum, staffCode: optional non-empty string.
 *
 * Note: the values here are the validated/normalized defaults; the consumer
 * (feedback-service) widens them to Partial<FeedbackListRequest>.
 */
export interface FeedbackListArgs {
  page: number;
  size: number;
  status?: 'PENDING' | 'PROCESSING' | 'RESOLVED' | 'CLOSED';
  staffCode?: string;
}

const FEEDBACK_LIST_MAX_SIZE = 100;

export const FeedbackListArgs = {
  parse(raw: unknown): ParseResult<FeedbackListArgs> {
    if (raw === undefined || raw === null) raw = {};
    if (!isPlainObject(raw)) return fail('args', 'expected object');

    // page: positive integer, default 1
    const pageRaw = (raw as Record<string, unknown>)['page'] ?? 1;
    if (typeof pageRaw !== 'number' || !Number.isFinite(pageRaw) || !Number.isInteger(pageRaw)) {
      return fail('page', 'expected positive integer');
    }
    if (pageRaw <= 0) return fail('page', 'must be >= 1');
    const page = pageRaw;

    // size: positive integer, default 20, capped at FEEDBACK_LIST_MAX_SIZE
    const sizeRaw = (raw as Record<string, unknown>)['size'] ?? 20;
    if (typeof sizeRaw !== 'number' || !Number.isFinite(sizeRaw) || !Number.isInteger(sizeRaw)) {
      return fail('size', 'expected positive integer');
    }
    if (sizeRaw <= 0) return fail('size', 'must be >= 1');
    if (sizeRaw > FEEDBACK_LIST_MAX_SIZE) {
      return fail('size', `must be <= ${FEEDBACK_LIST_MAX_SIZE}`);
    }
    const size = sizeRaw;

    const value: FeedbackListArgs = { page, size };

    // status: optional enum
    const statusRaw = (raw as Record<string, unknown>)['status'];
    if (statusRaw !== undefined) {
      if (
        statusRaw !== 'PENDING' &&
        statusRaw !== 'PROCESSING' &&
        statusRaw !== 'RESOLVED' &&
        statusRaw !== 'CLOSED'
      ) {
        return fail('status', 'must be PENDING|PROCESSING|RESOLVED|CLOSED');
      }
      value.status = statusRaw;
    }

    // staffCode: optional non-empty string
    const staffCodeRaw = (raw as Record<string, unknown>)['staffCode'];
    if (staffCodeRaw !== undefined) {
      const sc = StringSchema.parse(staffCodeRaw, 'staffCode');
      if (!sc.ok) return sc;
      value.staffCode = sc.value;
    }

    return { ok: true, value };
  },
};

/**
 * feedback:image args. path 只能是反馈服务同源下的相对路径
 * （如 'upload/xxx.png'）。主进程据此拼 baseURL 后 fetch，故 path 是信任边界。
 */
export interface FeedbackImageArgs {
  path: string;
}

export const FeedbackImageArgs = {
  parse(raw: unknown): ParseResult<FeedbackImageArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const path = StringSchema.parse(raw['path'], 'path');
    if (!path.ok) return path;
    if (path.value.length > 500) return fail('path', 'max 500 chars');
    if (
      !path.value
      || path.value.includes('..')
      || path.value.includes('\\')
      || path.value.startsWith('/')
      || path.value.includes('?')
      || path.value.includes('#')
      || /^[a-z][a-z0-9+.-]*:/i.test(path.value)
    ) {
      return fail('path', 'must be a same-origin relative path');
    }
    return { ok: true, value: { path: path.value } };
  },
};

/** dialog:selectFile args. */
export interface DialogSelectFileArgs {
  multiSelect?: boolean;
  filters?: Array<{ name: string; extensions: string[] }>;
  returnType?: 'paths' | 'dataUrl' | 'object';
  /** 单文件最大字节数（默认 MAX_DIALOG_FILE_BYTES）。 */
  maxBytes?: number;
}

export const DialogSelectFileArgs = {
  parse(raw: unknown): ParseResult<DialogSelectFileArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const multiSelect = BooleanSchema.parse(raw['multiSelect'] ?? false, 'multiSelect');
    if (!multiSelect.ok) return multiSelect;

    const filtersRaw = raw['filters'];
    let filters: DialogSelectFileArgs['filters'];
    if (filtersRaw !== undefined) {
      if (!Array.isArray(filtersRaw)) return fail('filters', 'expected array');
      filters = [];
      for (let i = 0; i < filtersRaw.length; i++) {
        const f = filtersRaw[i];
        if (!isPlainObject(f)) return fail(`filters[${i}]`, 'expected object');
        const name = StringSchema.parse(f['name'], `filters[${i}].name`);
        if (!name.ok) return name;
        const extRaw = f['extensions'];
        if (!Array.isArray(extRaw)) return fail(`filters[${i}].extensions`, 'expected array');
        const exts: string[] = [];
        for (let j = 0; j < extRaw.length; j++) {
          if (typeof extRaw[j] !== 'string') {
            return fail(`filters[${i}].extensions[${j}]`, 'expected string');
          }
          exts.push(extRaw[j]);
        }
        filters.push({ name: name.value, extensions: exts });
      }
    }

    const returnTypeRaw = raw['returnType'];
    let returnType: DialogSelectFileArgs['returnType'];
    if (returnTypeRaw !== undefined) {
      if (returnTypeRaw !== 'paths' && returnTypeRaw !== 'dataUrl' && returnTypeRaw !== 'object') {
        return fail('returnType', 'must be paths|dataUrl|object');
      }
      returnType = returnTypeRaw;
    }

    const maxBytesRaw = raw['maxBytes'];
    let maxBytes: number | undefined;
    if (maxBytesRaw !== undefined) {
      const n = NumberSchema.parse(maxBytesRaw, 'maxBytes');
      if (!n.ok) return n;
      if (n.value <= 0) return fail('maxBytes', 'must be > 0');
      if (n.value > MAX_DIALOG_FILE_BYTES) {
        return fail('maxBytes', `max ${MAX_DIALOG_FILE_BYTES}`);
      }
      maxBytes = n.value;
    }

    const value: DialogSelectFileArgs = { multiSelect: multiSelect.value };
    if (filters !== undefined) value.filters = filters;
    if (returnType !== undefined) value.returnType = returnType;
    if (maxBytes !== undefined) value.maxBytes = maxBytes;
    return { ok: true, value };
  },
};

/** dialog:selectFolder — 无参数，返回 string[] | null。 */
export const DialogSelectFolderArgs = {
  parse(raw: unknown): ParseResult<Record<string, never>> {
    if (raw === undefined || raw === null) return { ok: true, value: {} };
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    return { ok: true, value: {} };
  },
};

export interface DialogSaveLocalExportArgs {
  sourcePath: string;
  suggestedName: string;
}

/** 将后端在 Crew Home 内生成的 ZIP 复制到用户通过保存对话框选择的位置。 */
export const DialogSaveLocalExportArgs = {
  parse(raw: unknown): ParseResult<DialogSaveLocalExportArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const sourcePath = StringSchema.parse(raw['sourcePath'], 'sourcePath');
    if (!sourcePath.ok) return sourcePath;
    const suggestedName = StringSchema.parse(raw['suggestedName'], 'suggestedName');
    if (!suggestedName.ok) return suggestedName;
    if (!ABSOLUTE_PATH_RE.test(sourcePath.value)) return fail('sourcePath', 'must be an absolute path');
    if (suggestedName.value.includes('/') || suggestedName.value.includes('\\') || suggestedName.value.includes('\0')) {
      return fail('suggestedName', 'must be a plain filename');
    }
    if (!suggestedName.value.toLowerCase().endsWith('.zip')) return fail('suggestedName', 'must end with .zip');
    return { ok: true, value: { sourcePath: sourcePath.value, suggestedName: suggestedName.value } };
  },
};

export interface InspirationWindowArgs {
  inspirationId: string;
  title?: string;
}

/** 灵感悬浮窗只能打开后端生成的受控 site/canvas 标识，不能接收任意 URL。 */
export const InspirationWindowArgs = {
  parse(raw: unknown): ParseResult<InspirationWindowArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const inspirationId = StringSchema.parse(raw['inspirationId'], 'inspirationId');
    if (!inspirationId.ok) return inspirationId;
    if (!/^(?:site|canvas)_[0-9a-f]{12}$/i.test(inspirationId.value)) {
      return fail('inspirationId', 'must be a site_* or canvas_* id');
    }
    const title = OptionalStringSchema.parse(raw['title'], 'title');
    if (!title.ok) return title;
    if ((title.value || '').length > 200) return fail('title', 'max 200 chars');
    const value: InspirationWindowArgs = { inspirationId: inspirationId.value };
    if (title.value !== undefined) value.title = title.value;
    return { ok: true, value };
  },
};
