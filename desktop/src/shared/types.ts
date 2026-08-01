/**
 * 桌面端跨进程共享的纯类型定义。
 *
 * 这里只放类型（无运行时值），可在 main / preload / renderer / tests 任意侧 import。
 * 涉及业务对象的类型（如 ChatMessage / ToolCallInfo）仍保留在原模块，
 * 这里只放 IPC 协议相关的"最小必要"接口。
 */

/** 渲染层 IPC 错误的标准结构（zod 校验失败、handler 抛错都走此）。 */
export interface IpcError {
  /** 错误码：`IPC_ARG_VALIDATION_FAILED` / 业务自定义 / `INTERNAL_ERROR`。 */
  code: string;
  message: string;
  /** 调试用，详情。 */
  details?: unknown;
}

/** 通用返回：成功 / 失败二选一。 */
export type IpcResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: IpcError };

/** feedback service 提交结果（沿用既有返回结构）。 */
export interface FeedbackSubmitResult {
  success: boolean;
  message?: string;
  resultCode?: string;
  statusCode?: number;
}

/** desktop prefs（关闭行为）。 */
export type CloseBehavior = 'tray' | 'quit' | 'ask';

export interface DesktopPrefs {
  closeBehavior: CloseBehavior;
}

export interface AuthUserSnapshot {
  userId: string;
  phoneNumber: string;
  displayName?: string | undefined;
}

export interface AuthStateSnapshot {
  mode: 'unknown' | 'local' | 'remote' | 'dev';
  configured: boolean;
  providerId: string;
  isLoggedIn: boolean;
  user: AuthUserSnapshot | null;
}

/** 版本更新推送载荷（主进程 → renderer，经 preload onVersionUpdate）。 */
export interface VersionUpdatePayload {
  type: 'force' | 'reminder';
  title: string;
  message: string;
  version?: string | undefined;
  url?: string | undefined;
  reportedVersion: string;
}

export interface VersionUpdateDownloadProgressPayload {
  phase: 'downloading' | 'paused' | 'downloaded' | 'installing' | 'completed' | 'error';
  percent?: number | null | undefined;
  receivedBytes?: number | undefined;
  totalBytes?: number | undefined;
  message?: string | undefined;
}

export interface VersionUpdatePackageResult {
  success: boolean;
  message?: string | undefined;
  filePath?: string | undefined;
}

/**
 * update-state.json 落盘结构（主进程持久化，跨重启保留）。
 * - downloaded：已完整下载、可随时安装的包；安装成功 / 被更新版本抢占时清空。
 * - forceLock：force 阻断锁；当本机版本 >= requiredVersion 时清空（更新成功并重启后）。
 */
export interface DownloadedUpdateRecord {
  filePath: string;
  version: string;
  size: number;
  type: 'force' | 'reminder';
  message?: string | undefined;
}

export interface ForceLockRecord {
  requiredVersion: string;
  message?: string | undefined;
}

export interface UpdateStateSnapshot {
  downloaded: DownloadedUpdateRecord | null;
  forceLock: ForceLockRecord | null;
}
