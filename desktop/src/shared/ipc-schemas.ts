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
    let parsedUrl: URL;
    try {
      parsedUrl = new URL(urlResult.value);
    } catch {
      return fail('url', 'not a valid URL');
    }
    const hostname = parsedUrl.hostname;
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
      const headersRaw = initRaw['headers'];
      let headers: Record<string, string> | undefined;
      if (headersRaw !== undefined) {
        if (!isPlainObject(headersRaw)) return fail('init.headers', 'expected object');
        headers = {};
        for (const [k, v] of Object.entries(headersRaw)) {
          if (typeof v !== 'string') return fail(`init.headers.${k}`, 'expected string');
          headers[k] = v;
        }
      }
      const initObj: { method?: string; headers?: Record<string, string>; body?: string } = {};
      if (method.value !== undefined) initObj.method = method.value;
      if (body.value !== undefined) initObj.body = body.value;
      if (headers) initObj.headers = headers;
      init = initObj;
    }

    const value: GatewayFetchArgs = { url: urlResult.value };
    if (init !== undefined) value.init = init;
    return { ok: true, value };
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
    let parsedUrl: URL;
    try {
      parsedUrl = new URL(urlResult.value);
    } catch {
      return fail('url', 'not a valid URL');
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
      const f = filesRaw[i];
      if (typeof f !== 'string') return fail(`files[${i}]`, 'expected string');
      if (f.length === 0) return fail(`files[${i}]`, 'must be non-empty');
      if (f.length > 1024) return fail(`files[${i}]`, 'max 1024 chars');
      if (f.includes('\0')) return fail(`files[${i}]`, 'must not contain NUL');
      if (!ABSOLUTE_PATH_RE.test(f)) return fail(`files[${i}]`, 'must be an absolute path');
      files.push(f);
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
 * allowedRoot: 可选的额外允许根目录。用于项目工作空间场景：产物目录落在用户
 * 选择的项目文件夹下，需要显式授权该根目录。
 */
export interface ShellOpenPathArgs {
  path: string;
  allowedRoot?: string;
}

export const ShellOpenPathArgs = {
  parse(raw: unknown): ParseResult<ShellOpenPathArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const p = StringSchema.parse(raw['path'], 'path');
    if (!p.ok) return p;
    const value: ShellOpenPathArgs = { path: p.value };
    const allowedRoot = raw['allowedRoot'];
    if (allowedRoot !== undefined && allowedRoot !== null) {
      const r = StringSchema.parse(allowedRoot, 'allowedRoot');
      if (!r.ok) return r;
      value.allowedRoot = r.value;
    }
    return { ok: true, value };
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
    const pathResult = StringSchema.parse(raw['path'], 'path');
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
    const p = StringSchema.parse(raw['path'], 'path');
    if (!p.ok) return p;
    const content = raw['content'];
    if (typeof content !== 'string') return fail('content', `expected string, got ${typeof content}`);
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
    const p = StringSchema.parse(raw['path'], 'path');
    if (!p.ok) return p;
    const base64 = StringSchema.parse(raw['base64'], 'base64');
    if (!base64.ok) return base64;
    if (!/^[A-Za-z0-9+/]+={0,2}$/.test(base64.value)) return fail('base64', 'invalid base64');
    return { ok: true, value: { path: p.value, base64: base64.value } };
  },
};

/** update:start-download args — 客户端按 version + 当前 OS 拼下载 URL，故只需版本号。 */
export interface UpdateStartDownloadArgs {
  version: string;
  type: 'force' | 'reminder';
}

export const UpdateStartDownloadArgs = {
  parse(raw: unknown): ParseResult<UpdateStartDownloadArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const version = StringSchema.parse(raw['version'], 'version');
    if (!version.ok) return version;
    if (!version.value.trim()) return fail('version', 'must be non-empty');
    const rawType = raw['type'];
    if (rawType !== 'force' && rawType !== 'reminder') {
      return fail('type', 'must be force or reminder');
    }
    return { ok: true, value: { version: version.value.trim(), type: rawType } };
  },
};

export interface UpdateDownloadArgs {
  url: string;
}

export const UpdateDownloadArgs = {
  parse(raw: unknown): ParseResult<UpdateDownloadArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const url = StringSchema.parse(raw['url'], 'url');
    if (!url.ok) return url;
    let parsedUrl: URL;
    try {
      parsedUrl = new URL(url.value);
    } catch {
      return fail('url', 'not a valid URL');
    }
    if (parsedUrl.protocol !== 'http:' && parsedUrl.protocol !== 'https:') {
      return fail('url.protocol', 'must be http: or https:');
    }
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

/** feedback:submit args. */
export interface FeedbackSubmitArgs {
  title: string;
  description: string;
  images?: Array<{ name: string; dataUrl: string }>;
}

export const FeedbackSubmitArgs = {
  parse(raw: unknown): ParseResult<FeedbackSubmitArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const title = StringSchema.parse(raw['title'], 'title');
    if (!title.ok) return title;
    if (title.value.length > 200) return fail('title', 'max 200 chars');
    const description = StringSchema.parse(raw['description'], 'description');
    if (!description.ok) return description;
    if (description.value.length > 5000) return fail('description', 'max 5000 chars');

    const imagesRaw = raw['images'];
    let images: FeedbackSubmitArgs['images'];
    if (imagesRaw !== undefined) {
      if (!Array.isArray(imagesRaw)) return fail('images', 'expected array');
      if (imagesRaw.length > 9) return fail('images', 'max 9');
      images = [];
      for (let i = 0; i < imagesRaw.length; i++) {
        const item = imagesRaw[i];
        if (!isPlainObject(item)) return fail(`images[${i}]`, 'expected object');
        const name = StringSchema.parse(item['name'], `images[${i}].name`);
        if (!name.ok) return name;
        const dataUrl = StringSchema.parse(item['dataUrl'], `images[${i}].dataUrl`);
        if (!dataUrl.ok) return dataUrl;
        if (!dataUrl.value.startsWith('data:')) return fail(`images[${i}].dataUrl`, 'must be data URL');
        if (dataUrl.value.length > 5 * 1024 * 1024) {
          return fail(`images[${i}].dataUrl`, 'max 5MB per image');
        }
        images.push({ name: name.value, dataUrl: dataUrl.value });
      }
    }

    const value: FeedbackSubmitArgs = { title: title.value, description: description.value };
    if (images !== undefined) value.images = images;
    return { ok: true, value };
  },
};

/**
 * feedback:list args.
 * page: positive int (default 1), size: positive int capped at 100 (default 20),
 * status: optional enum.
 *
 * Note: the values here are the validated/normalized defaults; the consumer
 * (feedback-service) widens them to Partial<FeedbackListRequest>.
 */
export interface FeedbackListArgs {
  page: number;
  size: number;
  status?: 'PENDING' | 'PROCESSING' | 'RESOLVED' | 'CLOSED';
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

    return { ok: true, value };
  },
};

/** feedback:delete args. id: positive integer (服务端记录主键). */
export interface FeedbackDeleteArgs {
  id: number;
}

export const FeedbackDeleteArgs = {
  parse(raw: unknown): ParseResult<FeedbackDeleteArgs> {
    if (!isPlainObject(raw)) return fail('args', 'expected object');
    const idRaw = (raw as Record<string, unknown>)['id'];
    if (typeof idRaw !== 'number' || !Number.isFinite(idRaw) || !Number.isInteger(idRaw)) {
      return fail('id', 'expected positive integer');
    }
    if (idRaw <= 0) return fail('id', 'must be >= 1');
    return { ok: true, value: { id: idRaw } };
  },
};

/**
 * feedback:image args. path 为服务端 images 字段的相对路径(如 'upload/xxx.png')，
 * 或绝对 http(s) URL。主进程据此拼 baseURL 后 fetch，故 path 是信任边界：
 * 拒绝 '..'(防路径穿越)，绝对 URL 仅放行 http/https。
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
    if (path.value.includes('..')) return fail('path', 'must not contain ..');
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(path.value) && !/^https?:\/\//i.test(path.value)) {
      return fail('path', 'absolute URL must be http/https');
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
