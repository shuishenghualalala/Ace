/**
 * 桌面端 / 主进程 / 渲染层共用的常量。
 *
 * 注意：仅放无副作用的纯常量，不允许依赖 Node.js / Electron / DOM API，
 * 以保证可在 main、preload、renderer、tests 任意侧 import。
 */

/** 单文件上传 / 选择对话框的最大字节数（20 MB）。 */
export const MAX_DIALOG_FILE_BYTES = 20 * 1024 * 1024;

/** Nearby BLE 单文件上限；BLE 分片传输不适合无界文件。 */
export const MAX_NEARBY_FILE_BYTES = 4 * 1024 * 1024;

/** gateway:fetch 白名单协议 + hostname。 */
export const GATEWAY_FETCH_ALLOWED_HOSTNAMES: ReadonlySet<string> = new Set([
  '127.0.0.1',
  'localhost',
]);

/** gateway:fetch 允许的 path 前缀（只读本地 gateway REST）。 */
export const GATEWAY_FETCH_ALLOWED_PATH_PREFIXES: readonly string[] = ['/api/'];

/**
 * gateway:upload 允许的目标 pathname（精确匹配）。
 * 文件上传比 gateway:fetch 更敏感（主进程读本地文件并外发），
 * 因此不用 /api/ 前缀，而是逐端点放行；当前仅 Wiki 上传。
 */
export const GATEWAY_UPLOAD_ALLOWED_PATHS: ReadonlySet<string> = new Set(['/api/wiki/upload']);

/** gateway:upload 单文件最大字节数（200 MB）。后端 /api/wiki/upload 未设限，主进程读文件进内存需自限。 */
export const GATEWAY_UPLOAD_MAX_FILE_BYTES = 200 * 1024 * 1024;

/** gateway:upload 单次请求最大文件数。 */
export const GATEWAY_UPLOAD_MAX_FILES = 32;

/** shell:openExternal 允许的协议白名单。 */
export const OPEN_EXTERNAL_ALLOWED_PROTOCOLS: ReadonlySet<string> = new Set([
  'http:',
  'https:',
]);

/** safeStorage 不可用时回退到 base64 的提示阈值（用于日志）。 */
export const SAFE_STORAGE_UNAVAILABLE_TAG = 'safe_storage_unavailable';

/** IPC 参数校验失败的统一错误码（区别于业务错误）。 */
export const IPC_ARG_VALIDATION_FAILED = 'IPC_ARG_VALIDATION_FAILED';

/** 应用启动时 desktop prefs 路径名。 */
export const DESKTOP_PREFS_FILENAME = 'desktop-prefs.json';

/** 渲染层 notify 动画 / 移除时长（ms）。 */
export const NOTIFY_VISIBLE_MS = 1800;

/** 渲染层 notify 节流窗口（ms）：同一帧内的多次 notify 合并成 1 次 UI 批处理。 */
export const NOTIFY_THROTTLE_MS = 16;
