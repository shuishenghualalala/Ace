/**
 * Preload script - exposes Crew desktop APIs to the renderer.
 */
import { contextBridge, ipcRenderer } from 'electron';
import type {
  AuthStateSnapshot,
  UpdateStateSnapshot,
  VersionUpdateDownloadProgressPayload,
  VersionUpdatePackageResult,
  VersionUpdatePayload,
} from '../shared/types';
import type { SecurityAuditArgs } from '../shared/ipc-schemas';

const api = {
  windowMinimize: () => ipcRenderer.invoke('window:minimize'),
  windowMaximize: () => ipcRenderer.invoke('window:maximize'),
  windowClose: () => ipcRenderer.invoke('window:close'),
  windowIsMaximized: () => ipcRenderer.invoke('window:isMaximized'),
  appQuit: () => ipcRenderer.invoke('app:quit'),
  getAppVersion: () => ipcRenderer.invoke('app:get-version'),
  onMaximizedChanged: (cb: (max: boolean) => void) => {
    ipcRenderer.on('window:maximized-changed', (_e, max) => cb(max));
  },

  openExternal: (url: string) => ipcRenderer.invoke('shell:openExternal', { url }),
  openPath: (p: string, allowedRoot?: string) =>
    // 空串/空白 allowedRoot 会触发主进程 IPC 参数校验，统一归一化为 undefined
    ipcRenderer.invoke('shell:openPath', {
      path: p,
      allowedRoot: allowedRoot && allowedRoot.trim() ? allowedRoot : undefined,
    }),
  readTextFile: (p: string) => ipcRenderer.invoke('shell:readTextFile', { path: p }) as Promise<string>,
  /** 静默探测路径是否为可读文件；不存在时返回 false，不抛错、不刷主进程 ENOENT 日志。 */
  pathExists: (p: string) => ipcRenderer.invoke('shell:pathExists', { path: p }) as Promise<boolean>,
  /** 仅允许读取/覆盖当前账号已有的任务产物；主进程重新校验 canonical owner root。 */
  readFileBase64: (p: string) =>
    ipcRenderer.invoke('shell:readFileBase64', { path: p }) as Promise<{
      base64: string;
      mimeType: string;
    }>,
  writeTextFile: (p: string, content: string) =>
    ipcRenderer.invoke('shell:writeTextFile', { path: p, content }) as Promise<{ ok: true }>,
  writeFileBase64: (p: string, base64: string) =>
    ipcRenderer.invoke('shell:writeFileBase64', { path: p, base64 }) as Promise<{ ok: true }>,
  listOpenApplications: (p: string) =>
    ipcRenderer.invoke('shell:listOpenApplications', { path: p }) as Promise<Array<{
      id: string;
      name: string;
    }>>,
  openPathWith: (p: string, applicationId: string) =>
    ipcRenderer.invoke('shell:openPathWith', { path: p, applicationId }) as Promise<{ ok: true }>,
  /** 由主进程按已鉴权 Workspace 记录探测 root；Renderer 不提供路径。 */
  workspaceDirectoryInfo: (workspaceId: string) =>
    ipcRenderer.invoke('workspace:directoryInfo', { workspaceId }) as Promise<{
      exists: boolean;
      canonicalPath: string | null;
    }>,
  showItemInFolder: (p: string) => ipcRenderer.invoke('shell:showItemInFolder', { path: p }),
  copyImage: (p: string) => ipcRenderer.invoke('clipboard:writeImage', { path: p }) as Promise<{ ok: true }>,
  revealImage: (p: string) => ipcRenderer.invoke('image:showItemInFolder', { path: p }) as Promise<{ ok: true }>,
  selectFile: (opts?: Record<string, unknown>) => ipcRenderer.invoke('dialog:selectFile', opts || {}),
  selectFolder: () => ipcRenderer.invoke('dialog:selectFolder', {}),
  getAutoLaunchEnabled: () => ipcRenderer.invoke('app:get-auto-launch-enabled'),
  setAutoLaunchEnabled: (enabled: boolean) => ipcRenderer.invoke('app:set-auto-launch-enabled', enabled),
  getCloseBehavior: () => ipcRenderer.invoke('app:get-close-behavior'),
  setCloseBehavior: (behavior: 'tray' | 'quit' | 'ask') => ipcRenderer.invoke('app:set-close-behavior', behavior),
  getSystemLocale: () => ipcRenderer.invoke('app:get-system-locale') as Promise<string>,
  rendererInitialStateReady: (): Promise<{ ok: true }> =>
    ipcRenderer.invoke('app:renderer-initial-state-ready'),
  getStrictSecurityEnabled: () => ipcRenderer.invoke('security:get-strict-security'),
  setStrictSecurityEnabled: (enabled: boolean) =>
    ipcRenderer.invoke('security:set-strict-security', enabled),

  heartbeat: (version?: string) => ipcRenderer.invoke('auth:heartbeat', version),
  authGetState: () =>
    ipcRenderer.invoke('auth:get-state') as Promise<{ ok: boolean; state: AuthStateSnapshot; error?: string }>,
  authSendCode: (phoneNumber: string) =>
    ipcRenderer.invoke('auth:send-code', { phoneNumber }) as Promise<Record<string, unknown>>,
  authLogin: (identifier: string, code = '') =>
    ipcRenderer.invoke('auth:login', { identifier, code }) as Promise<Record<string, unknown>>,
  authLogout: () => ipcRenderer.invoke('auth:logout') as Promise<Record<string, unknown>>,

  // 反馈
  submitFeedback: (payload: { title: string; description: string; images?: Array<{ name: string; dataUrl: string }> }) =>
    ipcRenderer.invoke('feedback:submit', payload),
  getFeedbackList: (params: unknown) => ipcRenderer.invoke('feedback:list', params),
  // 附件图片：renderer 无法直连外部主机(CSP/webSecurity)，走主进程 fetch 转 data URL
  getFeedbackImage: (path: string) => ipcRenderer.invoke('feedback:image', { path }),

  // Gateway REST 桥接（webSecurity:true 下 renderer 不直连 127.0.0.1）
  // path 必须在主进程白名单内（/api/ 前缀），不允许指向外部主机
  ensureGateway: () => ipcRenderer.invoke('gateway:ensure'),
  getBackendStatus: (): Promise<{
    connected: boolean;
    baseUrl?: string;
    logPath?: string;
    components?: Record<string, { status: string; message?: string }>;
  }> => ipcRenderer.invoke('gateway:get-status'),
  retryGateway: () => ipcRenderer.invoke('gateway:retry'),
  gatewayFetch: (url: string, init?: { method?: string; headers?: Record<string, string>; body?: string }) =>
    ipcRenderer.invoke('gateway:fetch', { url, init: init || {} }),
  gatewayStreamStart: (
    requestId: string,
    url: string,
    init?: { method?: string; headers?: Record<string, string>; body?: string },
  ) => ipcRenderer.invoke('gateway:stream-start', {
    request_id: requestId,
    url,
    init: init || {},
  }),
  gatewayStreamCancel: (requestId: string) =>
    ipcRenderer.invoke('gateway:stream-cancel', { request_id: requestId }),
  onGatewayStreamEvent: (cb: (event: {
    request_id: string;
    type: 'head' | 'chunk' | 'end' | 'error';
    status?: number;
    headers?: Record<string, string>;
    text?: string;
    error?: string;
  }) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, event: Parameters<typeof cb>[0]) => cb(event);
    ipcRenderer.on('gateway:stream-event', listener);
    return () => ipcRenderer.removeListener('gateway:stream-event', listener);
  },
  securityPending: (args: { workspaceId: string; sessionId: string; taskId?: string }) =>
    ipcRenderer.invoke('security:pending', args),
  securitySetMode: (args: {
    workspaceId: string;
    sessionId: string;
    mode: 'request_approval' | 'auto_review' | 'full_access';
  }) => ipcRenderer.invoke('security:set-mode', args),
  securityDecide: (args: {
    workspaceId: string;
    sessionId: string;
    taskId?: string;
    requestId: string;
    decision: 'once' | 'session' | 'always' | 'reject';
    alwaysArgvPrefix?: string[];
  }) => ipcRenderer.invoke('security:decide', args),
  securityCapabilities: () => ipcRenderer.invoke('security:capabilities', {}),
  securityRules: (args: { workspaceId: string }) => ipcRenderer.invoke('security:rules', args),
  securitySetRule: (args: { workspaceId: string; ruleId: string; enabled: boolean }) =>
    ipcRenderer.invoke('security:set-rule', args),
  securityDeleteRule: (args: { workspaceId: string; ruleId: string }) =>
    ipcRenderer.invoke('security:delete-rule', args),
  securityAudit: (args: SecurityAuditArgs = {}) => ipcRenderer.invoke('security:audit', args),
  securityAuditExport: () => ipcRenderer.invoke('security:audit-export', {}),
  securityAuditPurge: (args: { workspaceId: string }) => ipcRenderer.invoke('security:audit-purge', args),
  securitySetup: (args: { action: 'install' | 'repair' | 'uninstall' }) =>
    ipcRenderer.invoke('security:setup', args),
  // 本地文件上传（gateway:fetch 只透传 string body，二进制走这里）：
  // files 为绝对路径数组，主进程读文件组 multipart POST；path 限 /api/wiki/upload。
  gatewayUpload: (url: string, files: string[]) =>
    ipcRenderer.invoke('gateway:upload', { url, files }),
  gatewayWsConnect: () => ipcRenderer.invoke('gateway-ws:connect'),
  gatewayWsSend: (payload: unknown) => ipcRenderer.invoke('gateway-ws:send', payload),
  gatewayWsClose: () => ipcRenderer.invoke('gateway-ws:close'),
  onGatewayWsEvent: (cb: (event: unknown) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, event: unknown) => cb(event);
    ipcRenderer.on('gateway-ws:event', listener);
    return () => ipcRenderer.removeListener('gateway-ws:event', listener);
  },

  // Browser state/debug remains an authenticated gateway channel. Page rendering
  // and takeover input stay inside the sandboxed native WebContentsView.
  browserWsConnect: (sessionId: string) => ipcRenderer.invoke('browser-ws:connect', sessionId),
  browserWsClose: () => ipcRenderer.invoke('browser-ws:close'),
  onBrowserWsEvent: (cb: (event: {
    type: string;
    sessionId?: string;
    data?: string;
    error?: string;
    code?: number;
    reason?: string;
  }) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, event: {
      type: string;
      sessionId?: string;
      data?: string;
      error?: string;
      code?: number;
      reason?: string;
    }) => cb(event);
    ipcRenderer.on('browser-ws:event', listener);
    return () => ipcRenderer.removeListener('browser-ws:event', listener);
  },
  browserViewSetPanel: (args: {
    sessionId: string;
    tabLabel: string;
    mode: 'ai' | 'human' | 'paused';
    bounds: { x: number; y: number; width: number; height: number };
    visible: boolean;
  }) => ipcRenderer.invoke('browser-view:set-panel', args),
  browserViewHide: () => ipcRenderer.invoke('browser-view:hide'),
  browserViewGetNavigation: (args: { sessionId: string; tabLabel: string }) =>
    ipcRenderer.invoke('browser-view:get-navigation', args) as Promise<{
      ok: boolean;
      navigation?: {
        url: string;
        title: string;
        can_go_back: boolean;
        can_go_forward: boolean;
      };
      error?: string;
    }>,
  onBrowserViewNavigationChanged: (cb: (event: { tabLabel: string }) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, value: { tabLabel: string }) => cb(value);
    ipcRenderer.on('browser-view:navigation-changed', listener);
    return () => ipcRenderer.removeListener('browser-view:navigation-changed', listener);
  },
  onBrowserViewLayoutInvalidated: (cb: () => void) => {
    const listener = () => cb();
    ipcRenderer.on('browser-view:layout-invalidated', listener);
    return () => ipcRenderer.removeListener('browser-view:layout-invalidated', listener);
  },

  // 版本更新事件订阅
  onVersionUpdate: (cb: (data: VersionUpdatePayload) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, data: VersionUpdatePayload) => cb(data);
    ipcRenderer.on('version-update-available', listener);
    return () => ipcRenderer.removeListener('version-update-available', listener);
  },
  onVersionUpdateDownloadProgress: (cb: (data: VersionUpdateDownloadProgressPayload) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, data: VersionUpdateDownloadProgressPayload) => cb(data);
    ipcRenderer.on('version-update-download-progress', listener);
    return () => ipcRenderer.removeListener('version-update-download-progress', listener);
  },
  // 版本更新：下载（客户端按 version+OS 拼 URL）/ 暂停 / 续传 / 重试 / 安装 / 读状态
  startDownload: (args: { version: string; type: 'force' | 'reminder'; url?: string | undefined }): Promise<{ success: boolean; message?: string }> =>
    ipcRenderer.invoke('update:start-download', args),
  pauseDownload: (): Promise<{ success: boolean }> => ipcRenderer.invoke('update:pause'),
  resumeDownload: (): Promise<{ success: boolean }> => ipcRenderer.invoke('update:resume'),
  retryDownload: (args: { version: string; type: 'force' | 'reminder'; url?: string | undefined }): Promise<{ success: boolean; message?: string }> =>
    ipcRenderer.invoke('update:retry', args),
  installUpdatePackage: (): Promise<VersionUpdatePackageResult> =>
    ipcRenderer.invoke('update:install-package'),
  getUpdateState: (): Promise<UpdateStateSnapshot> => ipcRenderer.invoke('update:get-state'),

  // 登录态单源（P1-3）：主进程推送当前登录态 + userInfo，renderer 据此驱动 UI
  onSessionState: (cb: (s: AuthStateSnapshot) => void) => {
    ipcRenderer.on('auth:session-state', (_e, s) => cb(s));
  },

  // 主进程未捕获错误（uncaughtException / unhandledRejection）：主进程不再 process.exit，
  // 改为把错误推到渲染层，这里接收后由 UI 弹 toast 提示用户「出错了但 app 还活着」。
  onMainUncaughtError: (cb: (err: { message: string; stack?: string }) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, err: { message: string; stack?: string }) => cb(err);
    ipcRenderer.on('main:uncaught-error', listener);
    return () => ipcRenderer.removeListener('main:uncaught-error', listener);
  },

  // 后端服务健康状态（主进程周期探测 /api/health）：
  // connected=true 表示 Gateway 已就绪可用，false 表示未连接/启动中。
  // logPath 为托管 gateway 启动日志路径，供「查看日志」诊断冷启动卡顿。
  onBackendStatus: (cb: (status: {
    connected: boolean;
    baseUrl?: string;
    logPath?: string;
    components?: Record<string, { status: string; message?: string }>;
  }) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, status: {
      connected: boolean;
      baseUrl?: string;
      logPath?: string;
      components?: Record<string, { status: string; message?: string }>;
    }) => cb(status);
    ipcRenderer.on('backend:status', listener);
    return () => ipcRenderer.removeListener('backend:status', listener);
  },

  // 卸载期间主进程发送此指令，强制隐藏后端断连遮罩（避免阻断卸载流程）
  onBackendSuppressOverlay: (cb: () => void) => {
    const listener = () => cb();
    ipcRenderer.on('backend:suppress-overlay', listener);
    return () => ipcRenderer.removeListener('backend:suppress-overlay', listener);
  },
};

contextBridge.exposeInMainWorld('Crew', api);

declare global {
  interface Window {
    Crew: typeof api;
  }
}
