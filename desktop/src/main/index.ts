/**
 * Electron main process entry
 */
// Windows 控制台默认 GBK：managed gateway 子进程的 UTF-8 中文/emoji 日志会被误解码成乱码。
// 把 main 进程 stdout/stderr 显式按 UTF-8 输出，并在 Windows 上切控制台代码页到 65001。
if (process.platform === 'win32') {
  try {
    process.stdout.setDefaultEncoding('utf8' as BufferEncoding);
    process.stderr.setDefaultEncoding('utf8' as BufferEncoding);
  } catch { /* default encoding best-effort */ }
}
import { app, BrowserWindow, ipcMain, shell, dialog, Tray, Menu, nativeImage, nativeTheme, protocol } from 'electron';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import * as net from 'net';
import { createHash, randomBytes, randomUUID } from 'crypto';
import { spawn, type ChildProcessWithoutNullStreams } from 'child_process';
import { serialize } from 'v8';
import WebSocket from 'ws';
import { appendPrivateSync } from './private-append';
import { BrowserHost, BrowserHostError } from './browser-host';
import { loginNewServiceInstance } from './auth-service';
import {
  feedbackServiceInstance,
  getFeedbackList,
  getFeedbackImage,
  type FeedbackConsentContext,
} from './feedback-service';
import {
  readResolvedCrewFile,
  registerCrewFileProtocol,
  resolveOwnedFilePath,
  type ResolvedOwnedFile,
} from './crew-file-protocol';
import { registerSitePreviewProtocol } from './site-preview-protocol';
import { authorizeOwnedImagePath, writeOwnedImageToClipboard } from './image-clipboard';
import {
  managedGatewayModeEnv,
  resolveGatewayCrewHome,
  resolveGatewayIdentityMode,
  shouldProbeExternalGateway,
  type GatewayIdentityMode,
} from './gateway-launch-mode';
import {
  resolveCrewHome,
} from './crew-session-file';
import {
  classifyCuaSetupAuthorityRequest,
  createDesktopSecurityProof,
  DESKTOP_REQUEST_ORIGIN,
  GATEWAY_INSTANCE_AUTH_HEADER,
  gatewayInstanceAccessToken,
  probeGatewayInstance,
  type GatewayComponentState,
} from './gateway-instance-auth';
import {
  chooseStandaloneGatewayAction,
  nextGatewayConnectionState,
  standaloneGatewayUsable,
  waitForGatewayCandidate,
} from './gateway-availability';
import { GatewayRestartController } from './gateway-restart-controller';
import { isTrustedRendererFileUrl } from './trusted-renderer-url';
import type {
  VersionUpdateDownloadProgressPayload,
  VersionUpdatePackageResult,
  UpdateStateSnapshot,
} from '../shared/types';
import {
  isStrictSecurityEnabled,
  normalizeCloseBehavior,
  readDesktopPrefsFile,
  saveCloseBehaviorPreference,
  saveStrictSecurityPreference,
  type CloseBehavior,
} from './desktop-prefs';
import { logMainStream } from './stream-debug';
import {
  confirmCuaDriverInstall,
  confirmDangerousAction,
  confirmFullAccessMode,
} from './host-authority-dialog';
import {
  GatewayFetchArgs,
  GatewayUploadArgs,
  ShellOpenExternalArgs,
  ShellOpenPathArgs,
  WorkspaceDirectoryArgs,
  ShellOpenPathWithArgs,
  ShellWriteFileBase64Args,
  ShellWriteTextFileArgs,
  FeedbackPreviewArgs,
  FeedbackSubmitArgs,
  FeedbackCancelArgs,
  FeedbackListArgs,
  FeedbackImageArgs,
  DialogSelectFileArgs,
  DialogSelectFolderArgs,
  DialogSaveLocalExportArgs,
  InspirationWindowArgs,
  UpdateStartDownloadArgs,
  SecurityPendingArgs,
  SecurityDecisionArgs,
  SecurityModeArgs,
  SecurityWorkspaceArgs,
  SecurityRuleMutationArgs,
  SecurityAlertActionArgs,
  SecurityAuditArgs,
  SecuritySetupArgs,
  WikiOpenSourceFileArgs,
} from '../shared/ipc-schemas';
import {
  getWindowsUacStatus,
  runElevatedSecuritySetup,
  runElevatedUacEnable,
} from './security-setup';
import { isDeniedShellPath } from './shell-allowed-path';
import {
  GATEWAY_UPLOAD_MAX_FILE_BYTES,
  IPC_ARG_VALIDATION_FAILED,
  MAX_DIALOG_FILE_BYTES,
} from '../shared/constants';
import {
  isIpcInvokeChannel,
  isIpcRendererToMainEventChannel,
  type IpcInvokeChannel,
  type IpcRendererToMainEventChannel,
} from '../shared/ipc-channels';
import { GatewayWsProtocolIdentity } from '../shared/gateway-ws-protocol';
import { listOpenWithApplications, openFileWithApplication } from './open-with-service';
import { handleUninstall, setUninstallDeps } from './uninstall';
import { currentAppVersion, currentAppVersionLabel } from './app-version';
import {
  configureUpdateController,
  startDownload as startUpdateDownload,
  pauseDownload as pauseUpdateDownload,
  resumeDownload as resumeUpdateDownload,
  retryDownload as retryUpdateDownload,
  disposeForQuit as disposeUpdateDownload,
  activeFilePaths,
} from './update/download-controller';
import {
  readUpdateState,
  setDownloadedRecord,
  setForceLock,
} from './update/update-state';
import {
  configuredUpdatePublicKey,
  verifyUpdateArtifact,
} from './update/update-integrity';
import { launchVerifiedDownloadedUpdate } from './update/update-installer';
import {
  ensurePrivateUpdateDirectory,
  removeManagedUpdateFile,
} from './update/update-file-security';
import { evaluateVersionUpdate } from './version-compare';
import { selectedFileAuthority } from './selected-file-authority';
import { resolveWorkspaceDirectoryInfo } from './workspace-directory';
import { configurePptxWasmRuntime, PPTX_WASM_V8_FLAGS } from './wasm-runtime';
import {
  hardenedChildProcessOptions,
  sanitizedChildProcessEnvironment,
} from './process-environment';

// 必须早于 app.whenReady()/BrowserWindow 创建；该开关随同一安装包跨 Windows、macOS、Linux 生效。
configurePptxWasmRuntime(app.commandLine);

const DEFAULT_GATEWAY_URL = 'http://127.0.0.1:8000';
const GATEWAY_PORT_MIN = 8000;
const GATEWAY_PORT_MAX = 8009; // inclusive upper bound, max 10 attempts

// A private standard scheme lets the sandboxed Browser WebContents render one
// explicitly approved HTML artifact without granting arbitrary file:// access.
protocol.registerSchemesAsPrivileged([
  {
    scheme: 'crew-artifact',
    privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true },
  },
  {
    // 对话 UI 内联展示任务工作区图片（browser_use screenshot 导出），
    // 边界校验见 main/crew-file-protocol.ts。
    scheme: 'crew-file',
    privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true },
  },
  {
    // Sites iframe 与其 HTML/JS/CSS/图片共享这个受控源，由主进程代理 Gateway 认证。
    scheme: 'ace-site',
    privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true, corsEnabled: true },
  },
]);

// Browser traffic must stay on the authenticated loopback policy proxy.
// These switches have to be applied before Chromium starts.
app.commandLine.appendSwitch('disable-quic');
app.commandLine.appendSwitch('force-webrtc-ip-handling-policy', 'disable_non_proxied_udp');

let mainWindow: BrowserWindow | null = null;
const inspirationWindows = new Map<string, BrowserWindow>();
const desktopFeedbackSessionId = randomUUID();
let managedGateway: ChildProcessWithoutNullStreams | null = null;
let managedGatewayInstanceKey: Buffer | null = null;
let ensureGatewayPromise: Promise<{ baseUrl: string; managed: boolean }> | null = null;
interface SecurityApprovalAuthority {
  nonce: string;
  workspaceId: string;
  sessionId: string;
  taskId: string;
  riskClass: string;
}

const securityApprovalAuthorities = new Map<string, SecurityApprovalAuthority>();
const gatewaySockets = new Map<number, WebSocket>();
const gatewaySocketProtocolIdentities = new WeakMap<WebSocket, GatewayWsProtocolIdentity>();
const gatewaySocketGenerations = new Map<number, number>();
const browserSockets = new Map<number, WebSocket>();
const browserSocketGenerations = new Map<number, number>();
let browserHost: BrowserHost | null = null;
let browserHostSocket: WebSocket | null = null;
let browserHostReconnectTimer: ReturnType<typeof setTimeout> | null = null;
let browserHostConnectionGeneration = 0;
let browserHostConnectPending = false;
let browserHostDisposePromise: Promise<void> | null = null;

function publicBrowserHostError(error: unknown): string {
  if (!(error instanceof BrowserHostError)) return '桌面浏览器操作失败';
  const code = /^[a-z0-9_-]{1,64}$/.test(error.code) ? error.code : 'browser_host_error';
  return `桌面浏览器操作失败（${code}）`;
}

const SAFE_WS_CLOSE_REASONS = new Set([
  'reconnect',
  'renderer-destroyed',
  'client-close',
  'switch-session',
  'invalid-browser-host-request',
  'browser-host-response-backpressure',
  'Protocol identity failed',
]);

function publicWebSocketCloseReason(reason: unknown): string {
  const text = Buffer.isBuffer(reason) ? reason.toString('utf8') : String(reason ?? '');
  return SAFE_WS_CLOSE_REASONS.has(text) ? text : 'connection-closed';
}
let tray: Tray | null = null;

// Renderer readiness reveal-gate.
//
// Default state is "open" (rendererInitialStateReady=true) so HEAD's existing
// show-on-ready-to-show behavior is unchanged. When the email-tenant-auth
// re-port wires resetRendererRevealGate() into createWindow(), the gate closes
// on cold start and reopens when the renderer signals app:renderer-initial-state-ready
// (or when RENDERER_READY_FALLBACK_MS elapses as a safety net).
let rendererInitialStateReady = true;
let nativeWindowReady = false;
let windowShowRequested = true;
let rendererReadyFallbackTimer: ReturnType<typeof setTimeout> | null = null;

const RENDERER_READY_FALLBACK_MS = 15_000;

let isQuitting = false;
let gatewayQuitCleanup: Promise<void> | null = null;
// Gateway 重建代际：每次主动重试/重建递增。在途 ensureGateway 流程启动时记下
// 自己的代际，health wait 每轮校验——代际变了说明用户点了重试，立即中止让位，
// 避免「旧 wait 无超时挂着、新重建又被排队」的假死。
let gatewayGeneration = 0;
// Backend health monitor state
let backendConnected = false;
let healthMonitorTimer: ReturnType<typeof setInterval> | null = null;
let healthPollInFlight = false;
let gatewayComponents: Record<string, GatewayComponentState> | undefined;
// Track the actually resolved gateway base URL (updated by ensureGateway)
let resolvedGatewayBaseUrl = DEFAULT_GATEWAY_URL;
// Idempotency guard for registerIpc().
// registerIpc() is called once at bootstrap, but defending against accidental
// re-invocation prevents duplicate ipcMain handlers (which would otherwise
// throw "attempt to register a second handler").
let ipcRegistered = false;

const GATEWAY_LAUNCH_SECRET_STDIN_ENV = 'ACE_GATEWAY_LAUNCH_SECRET_STDIN';

function deliverManagedGatewayLaunchKey(
  child: ChildProcessWithoutNullStreams,
  launchKey: Buffer,
): void {
  managedGatewayInstanceKey = Buffer.from(launchKey);
  child.stdin.on('error', (error) => {
    console.error('[gateway] failed to deliver launch identity:', error);
  });
  try {
    // Keep stdin open as a parent-liveness lease. The Gateway watches for EOF
    // and shuts itself down if Desktop exits or crashes.
    child.stdin.write(`${launchKey.toString('hex')}\n`, 'ascii');
  } finally {
    launchKey.fill(0);
  }
}

function clearManagedGatewayInstanceKey(): void {
  managedGatewayInstanceKey?.fill(0);
  managedGatewayInstanceKey = null;
}

function activeGatewayInstanceKey(baseUrl = resolvedGatewayBaseUrl): Buffer | undefined {
  return managedGateway
    && managedGatewayInstanceKey
    && baseUrl === resolvedGatewayBaseUrl
    ? Buffer.from(managedGatewayInstanceKey)
    : undefined;
}

const gatewayRestartController = new GatewayRestartController(async () => {
  if (isQuitting) return;
  gatewayGeneration += 1;
  ensureGatewayPromise = null;
  logSupervisorDecision('automatic-restart', { generation: gatewayGeneration });
  try {
    await ensureGateway();
    scheduleBrowserHostConnection();
  } catch (error) {
    console.error('[gateway] automatic restart failed:', error);
    throw error;
  }
});

function readCrewHomeFromConfig(): string | null {
  try {
    const configPath = path.join(repoRoot(), 'config', 'config.yaml');
    const content = fs.readFileSync(configPath, 'utf8');
    const match = content.match(/^\s*crew_home:\s*['"]?([^'"#\n]+?)['"]?\s*(?:#.*)?$/m);
    if (match?.[1]?.trim()) return match[1].trim();
  } catch {
    // config.yaml not available
  }
  return null;
}

function getTaskWorkspaceRoot(): string {
  if (process.env.CREW_TASK_WORKSPACE_ROOT) {
    return path.resolve(process.env.CREW_TASK_WORKSPACE_ROOT);
  }
  // 从 config.yaml 读取 crew_home，与后端 load_config() 保持一致
  const crewHome = readCrewHomeFromConfig();
  if (crewHome) {
    const resolved = path.isAbsolute(crewHome)
      ? crewHome
      : path.join(os.homedir(), crewHome);
    return path.join(resolved, 'task_workspaces');
  }
  // fallback: 与后端 home.py DEFAULT_HOME_DIRNAME 保持一致
  return path.join(os.homedir(), '.Crew', 'task_workspaces');
}

/**
 * 校验并解析可在 shell:openPath / shell:readTextFile 中访问的绝对路径。
 *
 * @param extraRoots 调用方显式授权的额外根目录（如项目工作空间根），必须是绝对路径。
 */
async function resolveShellAllowedPath(rawPath: string, extraRoots: string[] = []): Promise<string> {
  if (!path.isAbsolute(rawPath)) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: path must be absolute`);
  }
  const resolved = path.resolve(rawPath);
  // Deny sensitive security/audit resources even when they sit under an allowed root
  // (M-1): the instance HMAC key (.gateway-instance) and raw cross-owner SQLite must
  // never reach the renderer.
  if (isDeniedShellPath(resolved)) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: path is a sensitive security/audit resource`);
  }
  let canonical: string;
  try {
    canonical = await fs.promises.realpath(resolved);
  } catch {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: path does not exist`);
  }
  if (isDeniedShellPath(canonical)) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: path is a sensitive security/audit resource`);
  }
  const taskWorkspaceRoot = getTaskWorkspaceRoot();
  const rootCandidates = [
    app.getPath('userData'),
    app.getPath('downloads'),
    app.getPath('documents'),
    app.getPath('pictures'),
    app.getPath('desktop'),
    taskWorkspaceRoot,
    path.dirname(taskWorkspaceRoot),
    ...extraRoots,
  ].map((r) => path.resolve(r));
  const canonicalRoots = (await Promise.all(rootCandidates.map(async (root) => {
    try {
      return await fs.promises.realpath(root);
    } catch {
      return null;
    }
  }))).filter((root): root is string => root !== null);
  const comparable = (value: string): string =>
    process.platform === 'win32' ? value.toLowerCase() : value;
  const canonicalComparable = comparable(canonical);
  const allowed = canonicalRoots.some((root) => {
    const rootComparable = comparable(root);
    return (
      canonicalComparable === rootComparable
      || canonicalComparable.startsWith(`${rootComparable}${path.sep}`)
    );
  });
  if (!allowed) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: path not under any allowed root`);
  }
  return canonical;
}

const MANAGED_GATEWAY_PORT = 28180;
const MANAGED_GATEWAY_URL = `http://127.0.0.1:${MANAGED_GATEWAY_PORT}`;
const AUTOSTART_ARG = '--autostart';
const IS_DEV_LAUNCH = process.argv.includes('--dev');
const RENDERER_LAUNCH_SEARCH = `?launchMode=${IS_DEV_LAUNCH ? 'dev' : 'account'}`;
let gatewayIdentityMode: GatewayIdentityMode = 'local';

function usesDevGatewayIdentity(): boolean {
  return gatewayIdentityMode === 'dev';
}

/** Ace's local Gateway authenticates the loopback transport, not a remote JWT. */
function usesGatewayRemoteAuth(): boolean {
  return Boolean(loginNewServiceInstance.getJWTToken() && gatewayIdentityHeaders());
}

/**
 * Check whether a TCP port is available for binding.
 * Returns true if the port is free, false if occupied.
 * 🌟 启动优化：加 3s 超时保护，避免安全软件 Hook 导致 listen 永久挂起。
 */
function isPortAvailable(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try { server.close(); } catch { /* ignore */ }
      resolve(false);
    }, 3000);
    const server = net.createServer();
    server.once('error', () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(false);
    });
    server.listen(port, '127.0.0.1', () => {
      server.close(() => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(true);
      });
    });
  });
}

function gatewayIdentityHeaders(): Record<string, string> | null {
  // 远程/email 模式下鉴权完全由 crew_auth_session cookie 承担（Gateway 的
  // _remote_account_from_cookie 读它）。返回 Cookie 头；调用点拼出的 Bearer 被 Gateway 忽略。
  const cookie = loginNewServiceInstance.cookieHeader();
  if (!cookie) return null;
  return { Cookie: cookie };
}

function currentBrowserOwnerId(): string | null {
  if (usesDevGatewayIdentity()) return 'dev:dev';
  if (!loginNewServiceInstance.getSessionInfo().isLoggedIn) return null;
  // owner 形如 `<providerId>:<userId>`（remote/email），local 模式为 'local'。
  // 与 crew.gateway.auth 的 LOCAL_OWNER_ACCOUNT_ID / owner_account_id 对齐。
  return loginNewServiceInstance.ownerAccountId() ?? 'local';
}

function currentBrowserRuntimeKey(): string | null {
  const owner = currentBrowserOwnerId();
  if (!owner) return null;
  const digest = createHash('sha256').update(owner, 'utf8').digest('hex').slice(0, 12);
  return `crew_${digest}`;
}

function currentCrewFileOwnerSegment(): string | null {
  const owner = currentBrowserOwnerId();
  if (!owner) return null;
  const digest = createHash('sha256').update(owner, 'utf8').digest('hex').slice(0, 16);
  return `acct_${digest}`;
}

/** Authorize one existing renderer-selected artifact to the active account. */
function authorizeOwnedRendererFile(rawPath: string): ResolvedOwnedFile {
  const ownerSegment = currentCrewFileOwnerSegment();
  const resolved = ownerSegment
    ? resolveOwnedFilePath(rawPath, activeGatewayCrewHome(), ownerSegment)
    : null;
  if (!resolved) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: file is outside the current account`);
  }
  return resolved;
}

function resolvedFileIdentityMatches(resolved: ResolvedOwnedFile, stat: fs.Stats): boolean {
  return (
    stat.isFile()
    && stat.dev === resolved.identity.dev
    && stat.ino === resolved.identity.ino
    && stat.nlink === resolved.identity.nlink
    && stat.size === resolved.identity.size
    && stat.mtimeMs === resolved.identity.mtimeMs
    && stat.ctimeMs === resolved.identity.ctimeMs
  );
}

/**
 * Write through the already-authorized inode. Opening without O_TRUNC lets us
 * verify the inode before the first destructive operation.
 */
async function writeResolvedOwnedFile(
  resolved: ResolvedOwnedFile,
  content: string | Buffer,
): Promise<void> {
  const noFollow = process.platform !== 'win32' && typeof fs.constants.O_NOFOLLOW === 'number'
    ? fs.constants.O_NOFOLLOW
    : 0;
  const handle = await fs.promises.open(
    resolved.filePath,
    fs.constants.O_WRONLY | noFollow,
  );
  try {
    if (!resolvedFileIdentityMatches(resolved, await handle.stat())) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: file identity changed`);
    }
    await handle.truncate(0);
    await handle.writeFile(content);
    await handle.sync();
  } finally {
    await handle.close();
  }
}

/**
 * 背压熔断阈值。
 *
 * **按帧体积拒发是错的**（原来那样做过）：会静默丢掉完整快照和录制步骤，而
 * 那两样都是"少一条就对不上"的数据，本机 loopback 上按体积拒发纯粹是自找失败。
 * 所以正常一律发，不看单帧多大。
 *
 * 但缓冲无限积压同样不行：对端卡住（渲染进程忙、面板没在读）时 `socket.send`
 * 会一直往内存里堆，堆到主进程 OOM。阈值定得很高——只有对端真的停止消费才会
 * 撞到，一次正常的大快照（几百 KB）碰不到。
 */
const BROWSER_HOST_BACKPRESSURE_BYTES = 64 * 1024 * 1024;

function sendBrowserHostFrame(
  socket: WebSocket,
  value: Record<string, unknown>,
  _maxBytes?: number,
  _maxBufferedBytes?: number,
): boolean {
  if (socket.readyState !== WebSocket.OPEN) return false;
  // 只在缓冲严重积压时熔断：这不是"帧太大"，是"对端已经不读了"。
  if (socket.bufferedAmount > BROWSER_HOST_BACKPRESSURE_BYTES) return false;
  try {
    const payload = JSON.stringify(value);
    // ws.send queues frames in call order. Rejecting on payload size
    // silently dropped exact snapshots/recording steps and made
    // large Playwright responses fail despite a healthy local connection.
    socket.send(payload);
    return true;
  } catch {
    return false;
  }
}

function ensureBrowserHost(): BrowserHost {
  if (!browserHost) {
    browserHost = new BrowserHost(() => mainWindow);
    browserHost.on('debug', (event: unknown) => {
      if (!event || typeof event !== 'object' || Array.isArray(event)) return;
      const record = event as Record<string, unknown>;
      const runtimeKey = currentBrowserRuntimeKey();
      if (!runtimeKey || record.runtimeKey !== runtimeKey) return;
      const socket = browserHostSocket;
      if (!socket) return;
      sendBrowserHostFrame(socket, { type: 'event', event: record }, 64 * 1024, 256 * 1024);
    });
    browserHost.on('download', (event: unknown) => {
      if (!event || typeof event !== 'object' || Array.isArray(event)) return;
      const record = event as Record<string, unknown>;
      const runtimeKey = currentBrowserRuntimeKey();
      if (!runtimeKey || record.runtimeKey !== runtimeKey) return;
      const socket = browserHostSocket;
      if (!socket) return;
      sendBrowserHostFrame(socket, { type: 'event', event: record });
    });
    browserHost.on('recording', (event: unknown) => {
      if (!event || typeof event !== 'object' || Array.isArray(event)) return;
      const record = event as Record<string, unknown>;
      const markIncomplete = (): void => {
        browserHost?.markRecordingIncomplete(
          typeof record.targetId === 'string' ? record.targetId : '',
          typeof record.recordingId === 'string' ? record.recordingId : '',
        );
      };
      const runtimeKey = currentBrowserRuntimeKey();
      const v11 = record.schemaVersion === 11;
      if (
        !runtimeKey
        || (!v11 && record.runtimeKey !== runtimeKey)
        || (v11 && typeof record.runtimeKey === 'string' && record.runtimeKey !== runtimeKey)
      ) {
        markIncomplete();
        return;
      }
      const socket = browserHostSocket;
      if (!socket) {
        markIncomplete();
        return;
      }
      if (!sendBrowserHostFrame(socket, { type: 'event', event: record })) markIncomplete();
    });
    browserHost.on('tab-updated', (event: unknown) => {
      if (!event || typeof event !== 'object' || Array.isArray(event)) return;
      const record = event as Record<string, unknown>;
      const runtimeKey = currentBrowserRuntimeKey();
      if (!runtimeKey || record.runtimeKey !== runtimeKey || typeof record.label !== 'string') return;
      mainWindow?.webContents.send('browser-view:navigation-changed', {
        tabLabel: record.label,
      });
    });
    browserHost.on('user-interaction-requested', (event: unknown) => {
      if (!event || typeof event !== 'object' || Array.isArray(event)) return;
      const record = event as Record<string, unknown>;
      const runtimeKey = currentBrowserRuntimeKey();
      if (
        !runtimeKey
        || record.runtimeKey !== runtimeKey
        || typeof record.label !== 'string'
        || (record.source !== 'pointer' && record.source !== 'keyboard')
      ) return;
      mainWindow?.webContents.send('browser-view:interaction-requested', {
        tabLabel: record.label,
        source: record.source,
      });
    });
    browserHost.on('tab-load-failed', (event: unknown) => {
      if (!event || typeof event !== 'object' || Array.isArray(event)) return;
      const record = event as Record<string, unknown>;
      const runtimeKey = currentBrowserRuntimeKey();
      if (!runtimeKey || record.runtimeKey !== runtimeKey || typeof record.label !== 'string') return;
      mainWindow?.webContents.send('browser-view:load-failed', {
        tabLabel: record.label,
        url: typeof record.url === 'string' ? record.url : '',
        errorDescription: typeof record.errorDescription === 'string' ? record.errorDescription : '',
      });
    });
  }
  return browserHost;
}

function closeBrowserHostConnection(reason: string): void {
  browserHostConnectionGeneration += 1;
  if (browserHostReconnectTimer) {
    clearTimeout(browserHostReconnectTimer);
    browserHostReconnectTimer = null;
  }
  const socket = browserHostSocket;
  browserHostSocket = null;
  if (socket) {
    try { socket.close(1000, reason.slice(0, 100)); } catch { /* best effort */ }
  }
}

function scheduleBrowserHostConnection(delayMs = 0): void {
  const expectedRuntimeKey = currentBrowserRuntimeKey();
  if (isQuitting || !expectedRuntimeKey) return;
  if (browserHostDisposePromise) {
    const pendingDisposal = browserHostDisposePromise;
    const generation = browserHostConnectionGeneration;
    void pendingDisposal.then(() => {
      if (
        generation !== browserHostConnectionGeneration
        || currentBrowserRuntimeKey() !== expectedRuntimeKey
        || isQuitting
      ) return;
      scheduleBrowserHostConnection(delayMs);
    });
    return;
  }
  if (browserHostConnectPending) return;
  if (
    browserHostSocket?.readyState === WebSocket.OPEN
    || browserHostSocket?.readyState === WebSocket.CONNECTING
  ) return;
  if (browserHostReconnectTimer) return;
  browserHostReconnectTimer = setTimeout(() => {
    browserHostReconnectTimer = null;
    void connectBrowserHost();
  }, Math.max(0, delayMs));
}

async function connectBrowserHost(): Promise<void> {
  const expectedRuntimeKey = currentBrowserRuntimeKey();
  if (isQuitting || !expectedRuntimeKey || browserHostConnectPending || browserHostDisposePromise) return;
  if (
    browserHostSocket?.readyState === WebSocket.OPEN
    || browserHostSocket?.readyState === WebSocket.CONNECTING
  ) return;

  const generation = browserHostConnectionGeneration;
  let retryDelay: number | null = null;
  browserHostConnectPending = true;
  try {
    const ensured = await ensureGateway();
    if (generation !== browserHostConnectionGeneration || currentBrowserRuntimeKey() !== expectedRuntimeKey) {
      retryDelay = 0;
      return;
    }
    const target = new URL(ensured.baseUrl);
    target.protocol = target.protocol === 'https:' ? 'wss:' : 'ws:';
    target.pathname = '/ws/browser-host';
    target.search = '';
    target.hash = '';
    const socket = new WebSocket(target.toString(), {
      headers: gatewayAccessHeaders('/ws/browser-host'),
      // Keep the transport cap aligned with ElectronBridge's pre-JSON frame
      // budget; the RPC response path has a stricter 2 MiB application cap.
      maxPayload: 4 * 1024 * 1024,
    });
    browserHostSocket = socket;

    socket.on('message', (raw) => {
      if (socket !== browserHostSocket) return;
      const text = raw.toString();
      let request: Record<string, unknown>;
      try {
        const value: unknown = JSON.parse(text);
        if (!value || typeof value !== 'object' || Array.isArray(value)) {
          socket.close(1008, 'invalid-browser-host-request');
          return;
        }
        request = value as Record<string, unknown>;
      } catch {
        socket.close(1008, 'invalid-browser-host-request');
        return;
      }
      const id = typeof request.id === 'string' ? request.id : '';
      if (!/^[0-9a-f]{32}$/.test(id) || request.type !== 'request' || request.runtime_key !== expectedRuntimeKey) {
        socket.close(1008, 'invalid-browser-host-request');
        return;
      }
      void ensureBrowserHost().handleRpc(request).then((result) => {
        if (socket.readyState !== WebSocket.OPEN || socket !== browserHostSocket) return;
        const sent = sendBrowserHostFrame(
          socket,
          { type: 'response', id, ok: true, result },
          2 * 1024 * 1024,
          4 * 1024 * 1024,
        );
        if (!sent) {
          const failureSent = sendBrowserHostFrame(
            socket,
            { type: 'response', id, ok: false, error: '桌面浏览器响应过大或连接繁忙' },
            2 * 1024 * 1024,
            4 * 1024 * 1024,
          );
          if (!failureSent) {
            try { socket.close(1011, 'browser-host-response-backpressure'); } catch { /* connection changed */ }
          }
        }
      }).catch((error: unknown) => {
        if (socket.readyState !== WebSocket.OPEN || socket !== browserHostSocket) return;
        const failure = error instanceof BrowserHostError ? error : null;
        const sent = sendBrowserHostFrame(socket, {
          type: 'response',
          id,
          ok: false,
          error: publicBrowserHostError(error),
          // 传 code：Python 侧需要据此区分「ref 失效」这类可恢复失败与真正的故障，
          // 靠匹配中文错误文本太脆。
          code: failure?.code ?? '',
          uncertain: failure?.uncertain ?? false,
          phase: failure?.phase ?? '',
          partial: failure?.partial ?? false,
          completed_count: failure?.completed_count ?? 0,
          browser_stopped: failure?.browser_stopped ?? false,
          stop_unconfirmed: failure?.stop_unconfirmed ?? false,
        }, 2 * 1024 * 1024, 4 * 1024 * 1024);
        if (!sent) {
          try { socket.close(1011, 'browser-host-response-backpressure'); } catch { /* connection changed */ }
        }
      });
    });
    socket.on('close', () => {
      if (browserHostSocket === socket) browserHostSocket = null;
      if (generation === browserHostConnectionGeneration) {
        // A gateway restart loses BrowserManager's in-memory tab epoch. Drop
        // the local tab processes as well; the authenticated reconnect resets
        // the manager owner state while preserving the on-disk Profile.
        const staleHost = browserHost;
        browserHost = null;
        const disposal = staleHost?.dispose().catch(() => undefined) ?? Promise.resolve();
        browserHostDisposePromise = disposal;
        void disposal.then(() => {
          if (browserHostDisposePromise === disposal) browserHostDisposePromise = null;
          if (
            generation !== browserHostConnectionGeneration
            || currentBrowserRuntimeKey() !== expectedRuntimeKey
            || isQuitting
          ) return;
          scheduleBrowserHostConnection(1500);
        });
      }
    });
    socket.on('error', () => {
      // close drives the bounded reconnect; request contents are never logged.
    });
  } catch {
    if (generation === browserHostConnectionGeneration) retryDelay = 1500;
  } finally {
    browserHostConnectPending = false;
    if (retryDelay !== null) scheduleBrowserHostConnection(retryDelay);
  }
}

async function resetBrowserHost(reason: string): Promise<void> {
  closeBrowserHostConnection(reason);
  const pendingDisposal = browserHostDisposePromise;
  const host = browserHost;
  browserHost = null;
  if (pendingDisposal) await pendingDisposal.catch(() => undefined);
  if (host) await host.dispose().catch(() => undefined);
}

interface DesktopPrefs {
  closeBehavior: CloseBehavior;
  /**
   * Cold-start theme hint, set by the renderer whenever the user changes
   * the theme in Settings. Allows the BrowserWindow backgroundColor to
   * match the user's preference (avoids the dark→light or light→dark
   * flash on cold start — the FOUC the inline script on the renderer
   * side was added to fix).
   *   'system' — follow OS via nativeTheme.shouldUseDarkColors
   *   'light'  — force light window background
   *   'dark'   — force dark window background
   */
  themeMode?: 'system' | 'light' | 'dark';
}

// 全局兜底：记录到日志文件 + 标记退出码
function logFatal(context: string, err: unknown): void {
  try {
    const logDir = app.getPath('userData');
    fs.mkdirSync(logDir, { recursive: true, mode: 0o700 });
    const logPath = path.join(logDir, 'main-crash.log');
    const ts = new Date().toISOString();
    const msg = `[${ts}] [${context}]\n${err instanceof Error ? (err.stack ?? err.message) : String(err)}\n\n`;
    appendPrivateSync(logPath, msg);
  } catch {
    console.error('[main] failed to write crash log:', err);
  }
  console.error(`[main] ${context}:`, err);
}
process.on('unhandledRejection', (reason) => {
  logFatal('unhandledRejection', reason);
  // 推给渲染层展示（toast/log），不杀进程——单条 Promise 拒绝不应拖垮整 app。
  try {
    mainWindow?.webContents.send('main:uncaught-error', {
      message: reason instanceof Error ? reason.message : String(reason),
      stack: reason instanceof Error ? reason.stack : undefined,
    });
  } catch {
    // webContents 已死或没初始化——吞掉，让 logFatal 兜底
  }
});
process.on('uncaughtException', (err) => {
  logFatal('uncaughtException', err);
  // Electron 主进程设计上能容忍单 handler 失败——只 push 给渲染层展示，不再 exit。
  // 这样渲染进程仍能 reload 或继续运行，不会出现「先白屏、再被拖下水」的连锁效应。
  try {
    mainWindow?.webContents.send('main:uncaught-error', {
      message: err instanceof Error ? err.message : String(err),
      stack: err instanceof Error ? err.stack : undefined,
    });
  } catch {
    // swallow
  }
});

function loadDesktopPrefs(): DesktopPrefs {
  const parsed = readDesktopPrefsFile();
  const out: DesktopPrefs = {
    closeBehavior: normalizeCloseBehavior(parsed.closeBehavior),
  };
  if (parsed.themeMode === 'light' || parsed.themeMode === 'dark' || parsed.themeMode === 'system') {
    out.themeMode = parsed.themeMode;
  }
  return out;
}

function saveDesktopPrefs(next: DesktopPrefs): DesktopPrefs {
  try {
    return saveCloseBehaviorPreference(next.closeBehavior);
  } catch (err) {
    console.warn('[main] save desktop prefs failed:', err);
    return { closeBehavior: normalizeCloseBehavior(next.closeBehavior) };
  }
}

function getAutoLaunchArgs(): string[] {
  return app.isPackaged ? [AUTOSTART_ARG] : [app.getAppPath(), AUTOSTART_ARG];
}

function applyAutoLaunch(enabled: boolean): boolean {
  try {
    app.setLoginItemSettings({
      openAtLogin: enabled,
      path: process.execPath,
      args: getAutoLaunchArgs(),
    });
    return app.getLoginItemSettings().openAtLogin;
  } catch (err) {
    console.warn('[main] set auto launch failed:', err);
    return false;
  }
}

function getAutoLaunchEnabled(): boolean {
  try {
    return app.getLoginItemSettings().openAtLogin;
  } catch (err) {
    console.warn('[main] get auto launch failed:', err);
    return false;
  }
}

function isAutoStartLaunch(): boolean {
  return process.argv.includes(AUTOSTART_ARG);
}

function shouldLaunchHidden(): boolean {
  return isAutoStartLaunch();
}

function resolveAppIcon(): Electron.NativeImage {
  const iconPath = path.join(__dirname, '../assets/icon.png');
  const image = nativeImage.createFromPath(iconPath);
  return image.isEmpty() ? nativeImage.createEmpty() : image;
}

function resolveTrayIcon(): Electron.NativeImage {
  const image = resolveAppIcon();
  if (image.isEmpty()) return nativeImage.createEmpty();

  // macOS 菜单栏：图标需缩放到约 22x22 逻辑像素（@2x 44），
  // 并标记为 template image 以自动适配明暗模式。
  // 若不缩放，128x128 源图会以原始尺寸渲染，远大于其他菜单栏图标。
  if (process.platform === 'darwin') {
    return image.resize({ width: 22, height: 22 });
  }
  return image;
}

function revealMainWindowIfReady(): void {
  if (!mainWindow || !rendererInitialStateReady || !nativeWindowReady || !windowShowRequested) return;
  mainWindow.setSkipTaskbar(false);
  if (!mainWindow.isVisible()) mainWindow.show();
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
}

/**
 * Close the reveal-gate and arm the fallback timer.
 *
 * NOT wired into createWindow() by default -- HEAD shows the window on
 * ready-to-show without waiting for the renderer. The email-tenant-auth
 * re-port should call this in createWindow()/did-start-loading to hide the
 * window until the renderer resolves initial auth state.
 */
function resetRendererRevealGate(): void {
  rendererInitialStateReady = false;
  if (rendererReadyFallbackTimer) clearTimeout(rendererReadyFallbackTimer);
  rendererReadyFallbackTimer = setTimeout(() => {
    rendererReadyFallbackTimer = null;
    rendererInitialStateReady = true;
    console.warn('[main] renderer initial state readiness timed out; revealing window via fallback');
    revealMainWindowIfReady();
  }, RENDERER_READY_FALLBACK_MS);
  mainWindow?.hide();
}

function markRendererInitialStateReady(): void {
  rendererInitialStateReady = true;
  if (rendererReadyFallbackTimer) {
    clearTimeout(rendererReadyFallbackTimer);
    rendererReadyFallbackTimer = null;
  }
  revealMainWindowIfReady();
}

function showMainWindow(): void {
  if (!mainWindow) return;
  mainWindow.setSkipTaskbar(false);
  if (!mainWindow.isVisible()) mainWindow.show();
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.focus();
}

function hideMainWindowToTray(): void {
  if (!mainWindow) return;
  mainWindow.setSkipTaskbar(true);
  mainWindow.hide();
}

function confirmCloseBehavior(): CloseBehavior {
  const action = dialog.showMessageBoxSync(mainWindow!, {
    type: 'question',
    buttons: ['隐藏到托盘', '直接退出', '取消'],
    defaultId: 0,
    cancelId: 2,
    title: '关闭 Crew',
    message: '关闭窗口时，你希望 Crew 如何处理？',
    detail: '选择”隐藏到托盘”后，应用会继续在后台运行，可从系统托盘恢复。',
  });
  if (action === 0) return 'tray';
  if (action === 1) return 'quit';
  return 'ask';
}

function shouldPreventClose(): boolean {
  const prefs = loadDesktopPrefs();
  const behavior = prefs.closeBehavior === 'ask' ? confirmCloseBehavior() : prefs.closeBehavior;
  if (behavior === 'tray') {
    hideMainWindowToTray();
    return true;
  }
  if (behavior === 'quit') return false;
  return true;
}

function createTray(): void {
  if (tray) return;
  tray = new Tray(resolveTrayIcon());
  tray.setToolTip('Crew');
  tray.setContextMenu(
    Menu.buildFromTemplate([
      {
        label: '打开 Crew',
        click: () => showMainWindow(),
      },
      {
        label: '卸载',
        click: () => handleUninstall(),
      },
      { type: 'separator' },
      {
        label: '退出',
        click: () => {
          isQuitting = true;
          app.quit();
        },
      },
    ]),
  );
  tray.on('double-click', () => showMainWindow());
  tray.on('click', () => showMainWindow());
}

function pushSessionState(): void {
  if (usesDevGatewayIdentity()) {
    // 开发态：推送合成的已登录开发账号状态，让渲染进程登录墙不弹出。
    mainWindow?.webContents.send('auth:session-state', {
      mode: 'dev',
      configured: true,
      providerId: 'dev',
      isLoggedIn: true,
      user: { userId: 'dev', phoneNumber: '', displayName: '开发者' },
    });
    return;
  }
  mainWindow?.webContents.send('auth:session-state', loginNewServiceInstance.getState());
}

function closeGatewaySockets(reason = 'auth-state-changed'): void {
  for (const [id, generation] of gatewaySocketGenerations) {
    gatewaySocketGenerations.set(id, generation + 1);
  }
  for (const [id, generation] of browserSocketGenerations) {
    browserSocketGenerations.set(id, generation + 1);
  }
  for (const [id, socket] of gatewaySockets.entries()) {
    try {
      socket.close(1000, reason);
    } catch {
      // best-effort cleanup
    }
    gatewaySockets.delete(id);
  }
  for (const [id, socket] of browserSockets.entries()) {
    try {
      socket.close(1000, reason);
    } catch {
      // best-effort cleanup of the browser state/debug channel
    }
    browserSockets.delete(id);
  }
}

/**
 * Resolve the BrowserWindow backgroundColor for cold start (Task 6.1.2 /
 * D4.1.2). Mirrors the logic of the inline <head> script in index.html
 * so the first painted frame matches the user's theme — no dark→light
 * or light→dark flash before the renderer can apply data-theme.
 *
 * Source priority: stored desktop-prefs.themeMode (if any) → system
 * preference via nativeTheme.shouldUseDarkColors.
 */
function resolveWindowBackgroundColor(): string {
  const prefs = loadDesktopPrefs();
  let isDark: boolean;
  if (prefs.themeMode === 'dark') isDark = true;
  else if (prefs.themeMode === 'light') isDark = false;
  else isDark = nativeTheme.shouldUseDarkColors;
  return isDark ? '#000000' : '#ffffff';
}

function pushInspirationWindowState(inspirationId: string, open: boolean): void {
  if (isQuitting || !mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send('inspiration:window-state-changed', { inspirationId, open });
}

function openInspirationWindow(inspirationId: string, title = '灵感'): BrowserWindow {
  const existing = inspirationWindows.get(inspirationId);
  if (existing && !existing.isDestroyed()) {
    existing.show();
    existing.focus();
    pushInspirationWindowState(inspirationId, true);
    return existing;
  }
  const transparentSticky = process.platform === 'darwin';
  const win = new BrowserWindow({
    width: 420,
    height: 520,
    minWidth: 280,
    minHeight: 300,
    resizable: true,
    movable: true,
    alwaysOnTop: true,
    frame: false,
    transparent: transparentSticky,
    backgroundColor: transparentSticky ? '#00000000' : resolveWindowBackgroundColor(),
    roundedCorners: true,
    hasShadow: true,
    skipTaskbar: true,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    autoHideMenuBar: true,
    title: title || '灵感',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'inspiration-sticky-preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      webviewTag: false,
    },
  });
  win.setMenuBarVisibility(false);
  if (process.platform === 'darwin') {
    win.setAlwaysOnTop(true, 'floating');
    win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  }
  inspirationWindows.set(inspirationId, win);
  pushInspirationWindowState(inspirationId, true);
  const target = `ace-site://${encodeURIComponent(inspirationId)}/`;
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (event, url) => {
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== 'ace-site:' || parsed.hostname !== inspirationId) event.preventDefault();
    } catch {
      event.preventDefault();
    }
  });
  win.once('ready-to-show', () => win.show());
  win.on('closed', () => {
    if (inspirationWindows.get(inspirationId) !== win) return;
    inspirationWindows.delete(inspirationId);
    pushInspirationWindowState(inspirationId, false);
  });
  void win.loadURL(target);
  return win;
}

function closeInspirationWindow(inspirationId: string): boolean {
  const win = inspirationWindows.get(inspirationId);
  if (!win) return false;
  inspirationWindows.delete(inspirationId);
  if (!win.isDestroyed()) win.destroy();
  pushInspirationWindowState(inspirationId, false);
  return true;
}

function createWindow() {
  const launchHidden = shouldLaunchHidden();
  nativeWindowReady = false;
  windowShowRequested = !launchHidden;
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 960,
    minHeight: 640,
    resizable: true,
    frame: false,
    icon: resolveAppIcon(),
    backgroundColor: resolveWindowBackgroundColor(),
    title: 'Crew Desktop',
    show: !launchHidden,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      webviewTag: false,
      // Electron 36+ 不再把 app.commandLine 动态开关下传给渲染子进程。
      additionalArguments: [`--js-flags=${PPTX_WASM_V8_FLAGS.join(' ')}`],
    },
  });
  if (!browserHostDisposePromise) ensureBrowserHost();

  mainWindow.on('maximize', () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('window:maximized-changed', true);
    }
  });
  mainWindow.on('unmaximize', () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('window:maximized-changed', false);
    }
  });
  mainWindow.loadFile(path.join(__dirname, '../assets/index.html'));
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  mainWindow.webContents.on('will-navigate', (event, url) => {
    const expected = path.join(__dirname, '../assets/index.html');
    if (!isTrustedRendererFileUrl(url, expected)) event.preventDefault();
  });

  mainWindow.once('ready-to-show', () => {
    nativeWindowReady = true;
    if (launchHidden) {
      hideMainWindowToTray();
      return;
    }
    mainWindow?.show();
  });

  if (process.argv.includes('--dev')) {
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  }

  // 渲染崩溃/加载失败：自动 reload（带防抖防崩溃循环）。
  // 单 reload 而不是 process.exit：白屏体验可救，比让用户手动重开强太多。
  // 30s 窗口内最多 reload 3 次——超过就停手记日志，避免 GPU 进程 OOM 类问题的循环拉起。
  let reloadCount = 0;
  let firstReloadAt = 0;
  const RELOAD_WINDOW_MS = 30_000;
  const RELOAD_MAX_PER_WINDOW = 3;
  const tryAutoReload = (reason: string): void => {
    const now = Date.now();
    if (now - firstReloadAt > RELOAD_WINDOW_MS) {
      // 开新窗口：重置计数
      firstReloadAt = now;
      reloadCount = 0;
    }
    if (reloadCount >= RELOAD_MAX_PER_WINDOW) {
      logFatal(`auto-reload-exceeded (${reason})`, new Error('renderer reload 超过上限，已停止自动恢复'));
      return;
    }
    reloadCount += 1;
    console.warn(`[main] Auto-reloading renderer (${reloadCount}/${RELOAD_MAX_PER_WINDOW}) — reason: ${reason}`);
    if (mainWindow && !mainWindow.isDestroyed()) {
      browserHost?.hidePanel();
      mainWindow.webContents.reload();
    }
  };
  mainWindow.webContents.on('render-process-gone', (_e, details) => {
    selectedFileAuthority.clearRenderer(mainWindow!.webContents.id);
    securityApprovalAuthorities.clear();
    browserHost?.hidePanel();
    console.error('[main] Renderer crashed:', details);
    logFatal('render-process-gone', new Error(JSON.stringify(details)));
    // reason === 'crashed' | 'abnormal-exit' | 'launch-failed' | 'oom'
    // 后两者一般是渲染启动前就死了，reload 也救不回来——只对 crash/abnormal-exit 触发 reload
    if (details.reason === 'crashed' || details.reason === 'abnormal-exit') {
      tryAutoReload(`render-process-gone: ${details.reason}`);
    }
  });
  mainWindow.webContents.on('did-fail-load', (_e, code, desc, url, isMainFrame) => {
    // Inspector 的 HTML/PPT 预览会频繁创建和销毁子 frame。重绘期间取消
    // about:srcdoc 属于正常的 ERR_ABORTED，不是主窗口加载失败，也不应误报。
    if (!isMainFrame) {
      if (code !== -3) console.warn(`[main] Subframe failed to load ${url}: ${desc} (${code})`);
      return;
    }
    browserHost?.hidePanel();
    console.error(`[main] Failed to load ${url}: ${desc} (${code})`);
    // -3 ERR_ABORTED（用户主动 reload 引发）跳过；其它真实加载失败才 auto-reload
    if (code !== -3) {
      tryAutoReload(`did-fail-load: ${code} ${desc}`);
    }
  });
  mainWindow.webContents.on('did-start-navigation', (_event, url, _isInPlace, isMainFrame) => {
    if (isMainFrame) {
      selectedFileAuthority.clearRenderer(mainWindow!.webContents.id);
      securityApprovalAuthorities.clear();
    }
    const expected = path.join(__dirname, '../assets/index.html');
    if (!isMainFrame || !isTrustedRendererFileUrl(url, expected, RENDERER_LAUNCH_SEARCH)) return;
    browserHost?.hidePanel();
    resetRendererRevealGate();
  });
  // Cold-start session-state push. This MUST be registered inside
  // createWindow() (after `mainWindow` is assigned) — previously it lived in
  // registerIpc() which runs at bootstrap BEFORE createWindow(), so
  // `mainWindow` was null there and the optional chaining silently no-op'd,
  // meaning the auth:session-state push the renderer relies on never fired.
  mainWindow.webContents.on('did-finish-load', () => {
    pushSessionState();
    // Push current backend health status on load so the renderer knows immediately
    mainWindow?.webContents.send('backend:status', backendStatusPayload(backendConnected));
  });

  mainWindow.on('hide', () => browserHost?.hidePanel());
  mainWindow.on('show', () => {
    mainWindow?.webContents.send('browser-view:layout-invalidated');
  });
  mainWindow.on('closed', () => {
    selectedFileAuthority.clearRenderer(mainWindow!.webContents.id);
    securityApprovalAuthorities.clear();
    if (rendererReadyFallbackTimer) {
      clearTimeout(rendererReadyFallbackTimer);
      rendererReadyFallbackTimer = null;
    }
    feedbackServiceInstance.cancelAll();
    browserHost?.hidePanel();
    mainWindow = null;
  });
  mainWindow.on('close', (event) => {
    if (isQuitting) return;
    if (!mainWindow) return;
    if (!loginNewServiceInstance.getSessionInfo().isLoggedIn) {
      isQuitting = true;
      return;
    }
    if (!mainWindow.isVisible()) return;
    if (shouldPreventClose()) {
      event.preventDefault();
      return;
    }
    // AutomationHost owns a hidden BrowserWindow, so closing the visible main
    // window no longer makes Electron emit window-all-closed. A user-selected
    // "quit" must explicitly enter the app quit path and dispose hidden hosts.
    isQuitting = true;
    app.quit();
  });
}

function repoRoot(): string {
  // __dirname is desktop/dist/main, so we need three parent directories
  // to reach the repository root (project root where crew/ and .venv/ live).
  return path.resolve(__dirname, '..', '..', '..');
}

function candidatePython(): string {
  const root = repoRoot();
  const venvPython = process.platform === 'win32'
    ? path.join(root, '.venv', 'Scripts', 'python.exe')
    : path.join(root, '.venv', 'bin', 'python');
  if (fs.existsSync(venvPython)) return venvPython;
  return process.platform === 'win32' ? 'python.exe' : 'python3';
}

function activeGatewayCrewHome(): string {
  return resolveGatewayCrewHome(
    gatewayIdentityMode,
    resolveCrewHome(),
    path.join(app.getPath('userData'), 'gateway-dev'),
  );
}

function packagedSecurityRuntimeEnv(): Record<string, string> {
  const stateDir = path.join(app.getPath('userData'), 'security');
  const strictSecurity = '1';
  const runtimeName = process.platform === 'win32'
    ? 'ace-security-runtime.exe'
    : 'ace-security-runtime';
  const platformKey = `${process.platform}-${process.arch}`;
  const runtimeCandidates = app.isPackaged
    ? [path.join(process.resourcesPath, runtimeName)]
    : [
        path.join(repoRoot(), 'desktop', 'security-runtime-bin', runtimeName),
        path.join(repoRoot(), 'security-runtime', 'prebuilt', platformKey, runtimeName),
        path.join(repoRoot(), 'security-runtime', 'bin', runtimeName),
      ];
  const runtime = runtimeCandidates.find((candidate) => path.isAbsolute(candidate) && fs.existsSync(candidate)) ?? '';
  if (!runtime || !path.isAbsolute(runtime) || !fs.existsSync(runtime)) {
    return {
      ACE_SECURITY_STATE_DIR: stateDir,
      ACE_STRICT_SECURITY: strictSecurity,
    };
  }
  // Packaged builds keep the trust root inside app.asar, whose integrity is
  // enforced by Electron's embedded-ASAR fuse. The adjacent manifest remains
  // useful to the Python verifier, but it is not accepted as the Desktop trust
  // root because a same-user process could replace it together with the helper.
  const manifestPath = app.isPackaged
    ? path.join(app.getAppPath(), 'security-runtime-bin', 'runtime-manifest.json')
    : path.join(path.dirname(runtime), 'runtime-manifest.json');
  if (!fs.existsSync(manifestPath)) {
    return {
      ACE_SECURITY_STATE_DIR: stateDir,
      ACE_STRICT_SECURITY: strictSecurity,
    };
  }
  let trustedRuntimeDigest = '';
  let trustedRuntimeManifestDigest = '';
  let trustedBundledBwrapDigest = '';
  try {
    const manifestBytes = fs.readFileSync(manifestPath);
    const manifest = JSON.parse(manifestBytes.toString('utf8')) as {
      schema?: number;
      files?: Array<{ name?: string; sha256?: string }>;
      binary_name?: string;
      binary_sha256?: string;
      platform?: string;
      arch?: string;
    };
    const declaresPlatform = Boolean(manifest.platform || manifest.arch);
    if (
      declaresPlatform
      && (manifest.platform !== process.platform || manifest.arch !== process.arch)
    ) {
      return {
        ACE_SECURITY_STATE_DIR: stateDir,
        ACE_STRICT_SECURITY: strictSecurity,
      };
    }
    const runtimeRecords = manifest.files?.filter((item) => item.name === runtimeName) ?? [];
    const bwrapRecords = manifest.files?.filter((item) => item.name === 'bwrap') ?? [];
    const expected = runtimeRecords[0]?.sha256;
    const actual = createHash('sha256').update(fs.readFileSync(runtime)).digest('hex');
    if (
      manifest.schema !== 2
      || runtimeRecords.length !== 1
      || bwrapRecords.length > 1
      || manifest.binary_name !== runtimeName
      || manifest.binary_sha256 !== expected
      || !expected
      || !/^[a-f0-9]{64}$/.test(expected)
      || actual !== expected
      || (bwrapRecords.length === 1
        && !/^[a-f0-9]{64}$/.test(bwrapRecords[0]?.sha256 ?? ''))
    ) {
      return {
        ACE_SECURITY_STATE_DIR: stateDir,
        ACE_STRICT_SECURITY: strictSecurity,
      };
    }
    trustedRuntimeDigest = actual;
    trustedRuntimeManifestDigest = createHash('sha256')
      .update(manifestBytes)
      .digest('hex');
    trustedBundledBwrapDigest = bwrapRecords[0]?.sha256 ?? '';
  } catch {
    return {
      ACE_SECURITY_STATE_DIR: stateDir,
      ACE_STRICT_SECURITY: strictSecurity,
    };
  }
  return {
    ACE_SECURITY_STATE_DIR: stateDir,
    ACE_STRICT_SECURITY: strictSecurity,
    ...(app.isPackaged
      ? {
          ACE_DESKTOP_SECURITY_RUNTIME: runtime,
          ACE_DESKTOP_SECURITY_RUNTIME_SHA256: trustedRuntimeDigest,
          ACE_DESKTOP_SECURITY_RUNTIME_MANIFEST_SHA256: trustedRuntimeManifestDigest,
          ...(trustedBundledBwrapDigest
            ? { ACE_DESKTOP_BUNDLED_BWRAP_SHA256: trustedBundledBwrapDigest }
            : {}),
          ACE_SECURITY_RELEASE_MODE: '1',
        }
      : {}),
  };
}

/** Bind every sensitive Gateway request to this Desktop/Gateway installation. */
function gatewayAccessHeaders(
  pathname: string,
  method = 'GET',
  body: string | Buffer = '',
): Record<string, string> {
  const headers: Record<string, string> = {};
  const proofPath = new URL(pathname, 'http://127.0.0.1').pathname;
  const sessionCookie = loginNewServiceInstance.cookieHeader();
  if (sessionCookie) {
    headers.Cookie = sessionCookie;
    headers.Origin = DESKTOP_REQUEST_ORIGIN;
  }
  if (proofPath.startsWith('/api/') || proofPath.startsWith('/ws')) {
    headers[GATEWAY_INSTANCE_AUTH_HEADER] = createActiveGatewaySecurityProof(
      method,
      proofPath,
      body,
    );
  }
  if (proofPath.startsWith('/api/browser/') || proofPath.startsWith('/ws/browser')) {
    headers.Authorization = `Bearer ${gatewayInstanceAccessToken(
      activeGatewayCrewHome(),
      activeGatewayInstanceKey(),
    )}`;
  }
  return headers;
}

/** Sign requests with the same instance key as the active Gateway. */
function createActiveGatewaySecurityProof(
  method: string,
  pathname: string,
  body: string | Buffer,
): string {
  const instanceKey = activeGatewayInstanceKey();
  return createDesktopSecurityProof(method, pathname, body, {
    crewHome: activeGatewayCrewHome(),
    ...(instanceKey === undefined ? {} : { instanceKey }),
  });
}

loginNewServiceInstance.setGatewayProofProvider((method, pathname, body) => (
  createActiveGatewaySecurityProof(method, pathname, body)
));

async function probeHealthApi(baseUrl: string) {
  const instanceKey = activeGatewayInstanceKey(baseUrl);
  return probeGatewayInstance(baseUrl, {
    crewHome: activeGatewayCrewHome(),
    ...(instanceKey === undefined ? {} : { instanceKey }),
  });
}

async function gatewayCandidatePresent(baseUrl: string): Promise<boolean> {
  try {
    const parsed = new URL(baseUrl);
    const port = Number(parsed.port);
    if (parsed.protocol !== 'http:' || parsed.hostname !== '127.0.0.1' || !Number.isInteger(port)) {
      return false;
    }
    return !(await isPortAvailable(port));
  } catch {
    return false;
  }
}

// 供开发态 / 回退使用的 Gateway
function startManagedGateway(): void {
  if (managedGateway) return;
  const root = repoRoot();
  const python = candidatePython();
  const crewHome = activeGatewayCrewHome();
  const launchKey = randomBytes(32);
  const env = sanitizedChildProcessEnvironment({
    CREW_HOME: crewHome,
    GATEWAY_PORT: String(MANAGED_GATEWAY_PORT),
    PYTHONPATH: root,
    [GATEWAY_LAUNCH_SECRET_STDIN_ENV]: '1',
    ...packagedSecurityRuntimeEnv(),
    ...managedGatewayModeEnv(
      gatewayIdentityMode,
      crewHome,
    ),
    // Windows 控制台默认 GBK：强制 UTF-8，避免 Rich 日志写 emoji 时 UnicodeEncodeError 刷屏
    PYTHONIOENCODING: 'utf-8',
    ...(process.platform === 'win32' ? { PYTHONUTF8: '1' } : {}),
    // 不注入 CREW_TASK_WORKSPACE_ROOT，让后端从 config.yaml 自行计算
  });
  managedGateway = spawn(
    python,
    ['-m', 'crew.gateway.server'],
    hardenedChildProcessOptions(
      {
        cwd: root,
        windowsHide: true,
        detached: false,
        stdio: ['pipe', 'pipe', 'pipe'],
      },
      env,
    ),
  );
  const child = managedGateway;
  deliverManagedGatewayLaunchKey(child, launchKey);
  attachGatewayLog(child);
  writeGatewayLogLine(`[spawn] managed gateway pid=${child.pid} port=${MANAGED_GATEWAY_PORT} python=${python}`);
  child.stdout.on('data', (chunk) => console.log('[gateway]', String(chunk).trim()));
  child.stderr.on('data', (chunk) => console.warn('[gateway]', String(chunk).trim()));
  child.on('exit', (code, signal) => {
    console.warn('[gateway] exited', { code, signal });
    if (managedGateway === child) {
      managedGateway = null;
      clearManagedGatewayInstanceKey();
      ensureGatewayPromise = null;
      logSupervisorDecision('instance-exit', { platform: 'managed', code, signal });
      if (!isQuitting) gatewayRestartController.schedule();
    }
  });
  child.on('error', (error) => {
    console.error('[gateway] managed gateway process error:', error);
    if (managedGateway === child) {
      managedGateway = null;
      clearManagedGatewayInstanceKey();
      ensureGatewayPromise = null;
      logSupervisorDecision('instance-error', { platform: 'managed', error: String(error) });
      if (!isQuitting) gatewayRestartController.schedule();
    }
  });
}

// ============================================================================
// 🌟 核心新增：专供 Windows 打包态使用的绝对路径静默启动
// ============================================================================
/**
 * Windows 打包版专用：在指定端口启动 crew-gateway.exe。
 * 通过 GATEWAY_PORT 环境变量告知后端监听端口。
 */
function startWindowsPackagedGateway(port: number): void {
  if (managedGateway) return;

  const exeDir = path.dirname(app.getPath('exe'));
  const gatewayExePath = path.join(exeDir, '../crew-gateway/crew-gateway.exe');
  const gatewayDir = path.dirname(gatewayExePath);

  console.log(`[gateway] Starting packaged Windows gateway on port ${port}:`, gatewayExePath);

  const launchKey = randomBytes(32);
  try {
    const spawnStart = Date.now();
    managedGateway = spawn(gatewayExePath, [], hardenedChildProcessOptions(
      {
        cwd: gatewayDir,
        windowsHide: true,
        detached: false,
        stdio: ['pipe', 'pipe', 'pipe'],
      },
      {
        // 不注入 CREW_TASK_WORKSPACE_ROOT，让后端从 config.yaml 自行计算
        CREW_HOME: resolveCrewHome(),
        GATEWAY_PORT: String(port),
        [GATEWAY_LAUNCH_SECRET_STDIN_ENV]: '1',
        ...packagedSecurityRuntimeEnv(),
        // 与开发态一致：打包 exe 内嵌 Python 仍可能走 GBK 控制台
        PYTHONIOENCODING: 'utf-8',
        ...(process.platform === 'win32' ? { PYTHONUTF8: '1' } : {}),
      },
    ));
    const child = managedGateway;
    deliverManagedGatewayLaunchKey(child, launchKey);
    console.log(`[gateway] Spawn took ${Date.now() - spawnStart}ms, PID: ${child.pid}`);

    // 记录 stdout/stderr 用于诊断启动问题
    attachGatewayLog(child);
    writeGatewayLogLine(`[spawn] packaged win gateway pid=${child.pid} port=${port} exe=${gatewayExePath}`);
    child.stdout.on('data', (chunk) => console.log('[gateway-win]', String(chunk).trim()));
    child.stderr.on('data', (chunk) => console.warn('[gateway-win]', String(chunk).trim()));

    child.on('exit', (code, signal) => {
      console.warn('[gateway] Windows packaged gateway exited', { code, signal });
      // 若非正常退出，尝试读取 Python 侧写入的 crash log，把启动失败根因暴露出来
      if (code !== 0 && code !== null) {
        try {
          const crashLog = path.join(resolveCrewHome(), 'logs', 'gateway-crash.log');
          if (fs.existsSync(crashLog)) {
            const crashText = fs.readFileSync(crashLog, 'utf8').trim();
            if (crashText) {
              console.error('[gateway] Gateway crash log:\n', crashText);
            }
          }
        } catch { /* ignore read errors */ }
      }
      // retry 流程在杀进程前会先把 managedGateway 置 null；因此这里若仍非空，
      // 说明退出的是「当前」实例（自然退出/崩溃），可安全清理。若为 null 则是
      // retry 路径，已由 retry 接管，不动新代际状态。
      if (managedGateway === child) {
        managedGateway = null;
        clearManagedGatewayInstanceKey();
        ensureGatewayPromise = null;
        logSupervisorDecision('instance-exit', { platform: 'win32', code, signal });
        if (!isQuitting) gatewayRestartController.schedule();
      }
    });
    child.on('error', (err) => {
      console.error('[gateway] Failed to start Windows packaged gateway:', err);
      if (managedGateway === child) {
        managedGateway = null;
        clearManagedGatewayInstanceKey();
        ensureGatewayPromise = null;
        logSupervisorDecision('instance-error', { platform: 'win32', error: String(err) });
        if (!isQuitting) gatewayRestartController.schedule();
      }
    });
  } catch (err) {
    launchKey.fill(0);
    console.error('[gateway] Exception while spawning Windows gateway:', err);
  }
}

/**
 * macOS 打包版专用：在指定端口启动 crew-gateway 二进制。
 * macOS .app 结构：Ace.app/Contents/Resources/crew-gateway/
 */
function startMacOSPackagedGateway(port: number): void {
  if (managedGateway) return;

  // macOS .app bundle: exe 位于 Contents/MacOS/crew-desktop，
  // gateway 放在 Contents/Resources/crew-gateway/crew-gateway
  const resourcesPath = path.join(path.dirname(app.getPath('exe')), '..', 'Resources');
  const gatewayExePath = path.join(resourcesPath, 'crew-gateway', 'crew-gateway');
  const gatewayDir = path.dirname(gatewayExePath);

  console.log(`[gateway] Starting packaged macOS gateway on port ${port}:`, gatewayExePath);

  // Never kill by process name here: lifecycle cleanup is bound to the exact
  // ChildProcess instance so an unrelated same-user process cannot be targeted.

  const launchKey = randomBytes(32);
  try {
    managedGateway = spawn(gatewayExePath, [], hardenedChildProcessOptions(
      {
        cwd: gatewayDir,
        detached: false,
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
      },
      {
        CREW_HOME: resolveCrewHome(),
        GATEWAY_PORT: String(port),
        [GATEWAY_LAUNCH_SECRET_STDIN_ENV]: '1',
        ...packagedSecurityRuntimeEnv(),
      },
    ));
    const child = managedGateway;
    deliverManagedGatewayLaunchKey(child, launchKey);

    attachGatewayLog(child);
    writeGatewayLogLine(`[spawn] packaged mac gateway pid=${child.pid} port=${port} exe=${gatewayExePath}`);
    child.stdout.on('data', (chunk) => console.log('[gateway-mac]', String(chunk).trim()));
    child.stderr.on('data', (chunk) => console.warn('[gateway-mac]', String(chunk).trim()));

    child.on('exit', (code, signal) => {
      console.warn('[gateway] macOS packaged gateway exited', { code, signal });
      if (managedGateway === child) {
        managedGateway = null;
        clearManagedGatewayInstanceKey();
        ensureGatewayPromise = null;
        logSupervisorDecision('instance-exit', { platform: 'darwin', code, signal });
        if (!isQuitting) gatewayRestartController.schedule();
      }
    });
    child.on('error', (err) => {
      console.error('[gateway] Failed to start macOS packaged gateway:', err);
      if (managedGateway === child) {
        managedGateway = null;
        clearManagedGatewayInstanceKey();
        ensureGatewayPromise = null;
        logSupervisorDecision('instance-error', { platform: 'darwin', error: String(err) });
        if (!isQuitting) gatewayRestartController.schedule();
      }
    });
  } catch (err) {
    launchKey.fill(0);
    console.error('[gateway] Exception while spawning macOS gateway:', err);
  }
}

/**
 * Linux 打包版专用：在指定端口启动 crew-gateway 二进制。
 * deb 装在固定路径 /opt/crew-gateway/crew-gateway。
 *
 * 与 macOS/Windows 一致：desktop 子进程托管 gateway。废弃了原先的 systemd user
 * service 架构——多用户机器上各用户 systemd 服务都 enable-linger + Restart=always
 * 会抢同一个 8000 端口，且跨用户 instance key 不一致导致桌面端验签失败。
 * 改为 desktop 启动时 spawn、退出时随之消亡，彻底消除多用户常驻冲突。
 */
function startLinuxPackagedGateway(port: number): void {
  if (managedGateway) return;

  const gatewayExePath = '/opt/crew-gateway/crew-gateway';
  const gatewayDir = path.dirname(gatewayExePath);

  console.log(`[gateway] Starting packaged Linux gateway on port ${port}:`, gatewayExePath);

  const launchKey = randomBytes(32);
  try {
    managedGateway = spawn(gatewayExePath, [], hardenedChildProcessOptions(
      {
        cwd: gatewayDir,
        detached: false,
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
      },
      {
        CREW_HOME: resolveCrewHome(),
        GATEWAY_PORT: String(port),
        [GATEWAY_LAUNCH_SECRET_STDIN_ENV]: '1',
        ...packagedSecurityRuntimeEnv(),
      },
    ));
    const child = managedGateway;
    deliverManagedGatewayLaunchKey(child, launchKey);

    attachGatewayLog(child);
    writeGatewayLogLine(`[spawn] packaged linux gateway pid=${child.pid} port=${port} exe=${gatewayExePath}`);
    child.stdout.on('data', (chunk) => console.log('[gateway-linux]', String(chunk).trim()));
    child.stderr.on('data', (chunk) => console.warn('[gateway-linux]', String(chunk).trim()));

    child.on('exit', (code, signal) => {
      console.warn('[gateway] Linux packaged gateway exited', { code, signal });
      if (managedGateway === child) {
        managedGateway = null;
        clearManagedGatewayInstanceKey();
        ensureGatewayPromise = null;
        logSupervisorDecision('instance-exit', { platform: 'linux', code, signal });
        if (!isQuitting) gatewayRestartController.schedule();
      }
    });
    child.on('error', (err) => {
      console.error('[gateway] Failed to start Linux packaged gateway:', err);
      if (managedGateway === child) {
        managedGateway = null;
        clearManagedGatewayInstanceKey();
        ensureGatewayPromise = null;
        logSupervisorDecision('instance-error', { platform: 'linux', error: String(err) });
        if (!isQuitting) gatewayRestartController.schedule();
      }
    });
  } catch (err) {
    launchKey.fill(0);
    console.error('[gateway] Exception while spawning Linux gateway:', err);
  }
}

// ── 托管 Gateway 启动日志捕获 ──────────────────────────────────────────────
// 打包用户看不到主进程控制台；把 gateway stdout/stderr 落盘到
// userData/logs/gateway-startup.log，供「查看日志」诊断冷启动卡顿（AV 扫 cacert、
// 崩溃 traceback、端口冲突等）。每次 spawn 截断，只留本次尝试。
// spawn 与首次输出之间有窗口，先把 spawn 动作本身写进日志，避免开头长时间空白；
// 显式 utf8，避免 Windows GBK 控制台输出落到文件成乱码。
let gatewayLogStream: fs.WriteStream | null = null;

function gatewayLogPath(): string {
  return path.join(app.getPath('userData'), 'logs', 'gateway-startup.log');
}

function writeGatewayLogLine(line: string): void {
  try {
    gatewayLogStream?.write(`[${new Date().toISOString()}] ${line}\n`, 'utf8');
  } catch {
    /* 日志失败不阻断主流程 */
  }
}

function attachGatewayLog(child: ChildProcessWithoutNullStreams): void {
  try {
    const file = gatewayLogPath();
    const logDirectory = path.dirname(file);
    ensurePrivateUpdateDirectory(logDirectory);
    gatewayLogStream?.end();
    const noFollow = process.platform !== 'win32' && typeof fs.constants.O_NOFOLLOW === 'number'
      ? fs.constants.O_NOFOLLOW
      : 0;
    const fd = fs.openSync(
      file,
      fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_TRUNC | noFollow,
      0o600,
    );
    try {
      const info = fs.fstatSync(fd, { bigint: true });
      if (!info.isFile() || info.nlink !== 1n) throw new Error('unsafe gateway log target');
      if (process.platform !== 'win32') fs.fchmodSync(fd, 0o600);
      gatewayLogStream = fs.createWriteStream(file, {
        fd,
        encoding: 'utf8',
        autoClose: true,
      });
    } catch {
      fs.closeSync(fd);
      throw new Error('unsafe gateway log target');
    }
    const write = (prefix: string, chunk: Buffer | string): void => {
      const text = Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk);
      gatewayLogStream?.write(
        `[${new Date().toISOString()}] ${prefix}${text.endsWith('\n') ? text : `${text}\n`}`,
        'utf8',
      );
    };
    child.stdout.on('data', (c) => write('[out] ', c));
    child.stderr.on('data', (c) => write('[err] ', c));
    child.on('exit', (code, signal) => {
      writeGatewayLogLine(`[exit] code=${code} signal=${signal}`);
    });
  } catch {
    console.error('[gateway] failed to attach startup log');
  }
}

/** 统一 backend:status 负载：connected + 实际 baseUrl + 启动日志路径（供 renderer「查看日志」）。
 *  各平台 gateway 均由 desktop 子进程托管，gateway stdout/stderr 落盘到
 *  userData/logs/gateway-startup.log，「查看日志」指向它即可。 */
function backendLogInfo(): { logPath: string } {
  return { logPath: gatewayLogPath() };
}

function backendStatusPayload(connected: boolean): {
  connected: boolean;
  baseUrl: string;
  logPath: string;
  components?: Record<string, GatewayComponentState>;
} {
  return {
    connected,
    baseUrl: resolvedGatewayBaseUrl,
    ...backendLogInfo(),
    ...(gatewayComponents ? { components: gatewayComponents } : {}),
  };
}

/** supervisor 杀/拉决策持久化到启动日志（复盘报告 B5：重拉/终止动作须可回溯）。 */
function logSupervisorDecision(action: string, detail?: Record<string, unknown>): void {
  const extra = detail ? ` ${JSON.stringify(detail)}` : '';
  writeGatewayLogLine(`[decision] ${action}${extra}`);
  console.log(`[gateway] decision: ${action}${extra}`);
}

/**
 * 等到 /api/health 实例证明通过。
 *
 * 冷启动没有合理的时间天花板（AV 扫 cacert 可拖到 90s+，下一次可能更久）。
 * 默认一直等到：ready / 子进程退出 / 应用退出 / 所属 ensureGateway 代际被作废（用户重试）。
 * 仅 logout 受控重启等有外部 deadline 的路径传 timeoutMs。
 */
class GatewaySupersededError extends Error {
  constructor() { super('gateway wait superseded by retry'); }
}

async function waitForHealthApi(
  baseUrl: string,
  options: {
    timeoutMs?: number;
    process?: ChildProcessWithoutNullStreams | null;
    generation?: number;
  } = {},
): Promise<boolean> {
  const started = Date.now();
  let attempts = 0;
  const deadline = typeof options.timeoutMs === 'number'
    ? started + Math.max(0, options.timeoutMs)
    : null;
  while (true) {
    if (isQuitting) {
      console.warn(`[gateway] Health wait aborted (app quitting) after ${Date.now() - started}ms`);
      return false;
    }
    if (
      options.generation !== undefined
      && options.generation !== gatewayGeneration
    ) {
      console.warn(`[gateway] Health wait superseded after ${Date.now() - started}ms`);
      throw new GatewaySupersededError();
    }
    if (deadline !== null && Date.now() >= deadline) {
      console.warn(`[gateway] Health API timeout after ${Date.now() - started}ms (${attempts} attempts)`);
      return false;
    }
    const child = options.process;
    if (child && child.exitCode !== null) {
      console.warn(
        `[gateway] Health wait stopped: process exited code=${child.exitCode} after ${Date.now() - started}ms`,
      );
      return false;
    }
    attempts++;
    const probe = await probeHealthApi(baseUrl);
    if (probe.verified) {
      console.log(`[gateway] Health API ready after ${Date.now() - started}ms (${attempts} attempts)`);
      return true;
    }
    if (probe.status === 'untrusted') {
      console.error(`[gateway] Health API rejected an untrusted listener at ${baseUrl}`);
      return false;
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

/** Stop only a Gateway child owned by this Desktop before rebuilding it. */
async function stopManagedGateway(reason: string): Promise<void> {
  const child = managedGateway;
  if (!child) return;
  managedGateway = null;
  clearManagedGatewayInstanceKey();
  await new Promise<void>((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    if (child.exitCode !== null) {
      finish();
      return;
    }
    child.once('exit', finish);
    setTimeout(() => {
      try { child.kill('SIGKILL'); } catch { /* already dead */ }
      setTimeout(finish, 400);
    }, 8000);
    try { child.stdin.end(); } catch {
      try { child.kill(); } catch { finish(); }
    }
  });
  logSupervisorDecision('instance-exit-superseded', { pid: child.pid ?? -1, reason });
}

// ============================================================================
// Backend health monitor: periodically polls /api/health and pushes status
// changes to the renderer via IPC ('backend:status').
// ============================================================================
// 🌟 启动优化：健康探测间隔从 1500ms 降至 1000ms，更快感知 gateway 就绪状态变化
const HEALTH_CHECK_INTERVAL_MS = 1000;

function pushBackendStatus(connected: boolean, options: { force?: boolean } = {}): void {
  if (backendConnected === connected && !options.force) return;
  backendConnected = connected;
  console.log(`[main] backend status → ${connected ? 'connected' : 'disconnected'}`);
  try {
    mainWindow?.webContents.send('backend:status', backendStatusPayload(connected));
  } catch {
    // webContents may be destroyed
  }
}

async function pollBackendHealth(): Promise<void> {
  if (healthPollInFlight) return;
  healthPollInFlight = true;
  try {
    // Use the resolved gateway base URL (updated by ensureGateway after port selection).
    const baseUrl = resolvedGatewayBaseUrl;
    const probe = await probeHealthApi(baseUrl);
    if (probe.verified) {
      const componentsChanged = JSON.stringify(gatewayComponents) !== JSON.stringify(probe.components);
      gatewayComponents = probe.components;
      pushBackendStatus(true, { force: componentsChanged });
      return;
    }

    // A busy asyncio loop can miss health deadlines while its TCP listener remains owned by
    // the same Gateway. Preserve the last verified state in that case. Only an explicit bad
    // proof or a listener that has actually disappeared may turn the UI disconnected.
    const candidatePresent = probe.status === 'untrusted'
      ? true
      : await gatewayCandidatePresent(baseUrl);
    const nextConnected = nextGatewayConnectionState(
      backendConnected,
      probe,
      candidatePresent,
    );
    if (!nextConnected) gatewayComponents = undefined;
    pushBackendStatus(nextConnected);
  } finally {
    healthPollInFlight = false;
  }
}

function startBackendHealthMonitor(): void {
  if (healthMonitorTimer) return;
  // Immediate first check
  void pollBackendHealth();
  healthMonitorTimer = setInterval(() => {
    void pollBackendHealth();
  }, HEALTH_CHECK_INTERVAL_MS);
}

function stopBackendHealthMonitor(): void {
  if (healthMonitorTimer) {
    clearInterval(healthMonitorTimer);
    healthMonitorTimer = null;
  }
  healthPollInFlight = false;
}

// ============================================================================
// 🌟 核心改造：双平台极致适配的 Gateway 路由
// ============================================================================
async function ensureGateway(): Promise<{ baseUrl: string; managed: boolean }> {
  const cachedPromise = ensureGatewayPromise;
  if (cachedPromise) {
    const cached = await cachedPromise;
    // A prior proof is not a permanent trust grant. Re-prove immediately before
    // every credential-bearing caller reuses the URL, so an unmanaged Gateway
    // restart cannot silently turn a stale cached port into a trusted service.
    // Timeout means "not answering now", not "wrong instance". Keep waiting for the same
    // candidate while its process/listener exists; this prevents expensive tools or deferred
    // startup from being mistaken for an identity change and recycled in a loop.
    const firstProbe = await probeHealthApi(cached.baseUrl);
    if (standaloneGatewayUsable(firstProbe)) return cached;

    const waitGeneration = gatewayGeneration;
    const ownedChild = cached.managed ? managedGateway : null;
    const candidatePresent = cached.managed
      ? Boolean(ownedChild && ownedChild.exitCode === null)
      : await gatewayCandidatePresent(cached.baseUrl);

    if (firstProbe.status === 'untrusted' && candidatePresent) {
      if (cached.managed) await stopManagedGateway('identity-rejected');
      if (ensureGatewayPromise === cachedPromise) ensureGatewayPromise = null;
      throw new Error(`Gateway instance verification rejected ${cached.baseUrl}`);
    }

    if (candidatePresent) {
      logSupervisorDecision('wait-existing-instance', {
        baseUrl: cached.baseUrl,
        managed: cached.managed,
      });
      const waited = await waitForGatewayCandidate({
        probe: () => probeHealthApi(cached.baseUrl),
        shouldContinue: async () => {
          if (
            isQuitting
            || gatewayGeneration !== waitGeneration
            || ensureGatewayPromise !== cachedPromise
          ) return false;
          if (cached.managed) {
            return managedGateway === ownedChild && ownedChild?.exitCode === null;
          }
          return gatewayCandidatePresent(cached.baseUrl);
        },
      });
      if (waited.status === 'ready' && standaloneGatewayUsable(waited.probe)) return cached;
      if (waited.status === 'untrusted') {
        if (cached.managed) await stopManagedGateway('identity-rejected');
        if (ensureGatewayPromise === cachedPromise) ensureGatewayPromise = null;
        throw new Error(`Gateway instance verification rejected ${cached.baseUrl}`);
      }
      if (waited.status === 'timeout') {
        logSupervisorDecision('wait-existing-instance-timeout', {
          baseUrl: cached.baseUrl,
          managed: cached.managed,
        });
      }
    }

    // The candidate really disappeared. Clear only the promise we inspected, then rebuild.
    if (ensureGatewayPromise !== cachedPromise) return ensureGateway();
    ensureGatewayPromise = null;
    logSupervisorDecision('instance-gone', {
      cachedBaseUrl: cached.baseUrl,
      managed: cached.managed,
    });
    return ensureGateway();
  }
  const ensureStart = Date.now();
  const generation = gatewayGeneration;
  const pending = (async (): Promise<{ baseUrl: string; managed: boolean }> => {
    // 若启动前代际已被作废（如点击重试的间隙），立即让位。
    if (generation !== gatewayGeneration) throw new GatewaySupersededError();
    // Windows 打包版：跳过默认端口探测，直接走端口扫描+启动 EXE 路径
    // 避免对不存在的 8000 端口发起 HTTP 请求浪费 1-2 秒
    if (app.isPackaged && process.platform === 'win32') {
      // 托管实例仍存活（缓存重证明连续失败后的重建）→ 直接复用并等待，
      // 不扫端口不 spawn；旧实例退出后 wait 立即返回 false，走下面正常重建。
      if (managedGateway) {
        console.log('[gateway] Windows packaged: managed instance alive, waiting on existing port...');
        logSupervisorDecision('reuse-alive-instance', { platform: 'win32' });
        if (await waitForHealthApi(resolvedGatewayBaseUrl, { process: managedGateway, generation })) {
          return { baseUrl: resolvedGatewayBaseUrl, managed: true };
        }
      }
      console.log('[gateway] Windows packaged: starting port scan...');
      let selectedPort = GATEWAY_PORT_MIN;
      for (let port = GATEWAY_PORT_MIN; port <= GATEWAY_PORT_MAX; port++) {
        const available = await isPortAvailable(port);
        if (available) {
          selectedPort = port;
          break;
        }
        if (port === GATEWAY_PORT_MAX) {
          console.error(`[gateway] All ports ${GATEWAY_PORT_MIN}-${GATEWAY_PORT_MAX} are in use, giving up`);
          throw new Error('no available port for the packaged Crew Gateway');
        }
        console.log(`[gateway] Port ${port} is in use, trying ${port + 1}...`);
      }
      const portUrl = `http://127.0.0.1:${selectedPort}`;
      console.log(`[gateway] Port scan done in ${Date.now() - ensureStart}ms, selected ${selectedPort}`);
      // spawn 前先把 healthMonitor 指向即将监听的端口；wait 期间监控才能打对地址。
      // 托管实例无冷启动时间天花板：等到 ready / 子进程退出 / 关应用 / 代际作废。
      resolvedGatewayBaseUrl = portUrl;
      startWindowsPackagedGateway(selectedPort);
      if (await waitForHealthApi(portUrl, { process: managedGateway, generation, timeoutMs: 60_000 })) {
        console.log(`[gateway] Total ensureGateway time: ${Date.now() - ensureStart}ms`);
        return { baseUrl: portUrl, managed: true };
      }
      throw new Error('packaged Windows Crew Gateway exited before readiness');
    }

    // 本地启动优先复用 8000 上由用户单独运行的 Gateway。
    // 所有打包态都由本进程 spawn 并通过匿名 stdin 管道交付本次启动密钥；
    // 不复用只持有持久安装密钥的外部 Gateway。
    if (
      shouldProbeExternalGateway(gatewayIdentityMode)
      && !app.isPackaged
    ) {
      const firstProbe = await probeHealthApi(DEFAULT_GATEWAY_URL);
      const externalPresent = firstProbe.verified
        ? true
        : await gatewayCandidatePresent(DEFAULT_GATEWAY_URL);
      const externalAction = chooseStandaloneGatewayAction(firstProbe, externalPresent);
      if (externalAction === 'reuse') {
        if (managedGateway) await stopManagedGateway('switch-to-external');
        resolvedGatewayBaseUrl = DEFAULT_GATEWAY_URL;
        return { baseUrl: DEFAULT_GATEWAY_URL, managed: false };
      }
      if (externalAction === 'reject') {
        // Never hide a trust failure by silently launching a second Gateway elsewhere.
        throw new Error(
          `Existing service at ${DEFAULT_GATEWAY_URL} failed Gateway instance verification; `
          + 'ensure the standalone Gateway and Desktop use the same CREW_HOME',
        );
      }
      if (externalAction === 'wait') {
        logSupervisorDecision('wait-external-instance', { baseUrl: DEFAULT_GATEWAY_URL });
        const waited = await waitForGatewayCandidate({
          probe: () => probeHealthApi(DEFAULT_GATEWAY_URL),
          shouldContinue: async () => {
            if (isQuitting || generation !== gatewayGeneration) return false;
            return gatewayCandidatePresent(DEFAULT_GATEWAY_URL);
          },
        });
        if (waited.status === 'ready' && standaloneGatewayUsable(waited.probe)) {
          if (managedGateway) await stopManagedGateway('switch-to-external');
          resolvedGatewayBaseUrl = DEFAULT_GATEWAY_URL;
          return { baseUrl: DEFAULT_GATEWAY_URL, managed: false };
        }
        if (waited.status === 'untrusted') {
          throw new Error(
            `Existing service at ${DEFAULT_GATEWAY_URL} failed Gateway instance verification; `
            + 'ensure the standalone Gateway and Desktop use the same CREW_HOME',
          );
        }
        if (waited.status === 'timeout') {
          logSupervisorDecision('wait-external-instance-timeout', {
            baseUrl: DEFAULT_GATEWAY_URL,
          });
        }
        if (generation !== gatewayGeneration) throw new GatewaySupersededError();
      }
    }

    // 3. Linux 打包版专属：desktop 子进程托管 gateway（与 Win/mac 一致）
    // 废弃了原先依赖 systemd user service 的架构：多用户机器上各用户 systemd 服务
    // enable-linger + Restart=always 会抢同一个 8000，且跨用户 instance key 不一致
    // 导致验签失败。改为 desktop 启动 spawn、退出随之消亡，彻底消除多用户常驻冲突。
    if (app.isPackaged && process.platform === 'linux') {
      // 托管实例存活时复用，避免扫出新端口却 spawn 不出来空等。
      if (managedGateway) {
        console.log('[gateway] Linux packaged: managed instance alive, waiting on existing port...');
        logSupervisorDecision('reuse-alive-instance', { platform: 'linux' });
        if (await waitForHealthApi(resolvedGatewayBaseUrl, { process: managedGateway, generation })) {
          return { baseUrl: resolvedGatewayBaseUrl, managed: true };
        }
      }
      let selectedPort = GATEWAY_PORT_MIN;
      for (let port = GATEWAY_PORT_MIN; port <= GATEWAY_PORT_MAX; port++) {
        const available = await isPortAvailable(port);
        if (available) {
          selectedPort = port;
          break;
        }
        if (port === GATEWAY_PORT_MAX) {
          console.error(`[gateway] All ports ${GATEWAY_PORT_MIN}-${GATEWAY_PORT_MAX} are in use, giving up`);
          throw new Error('no available port for the packaged Crew Gateway');
        }
        console.log(`[gateway] Port ${port} is in use, trying ${port + 1}...`);
      }
      const portUrl = `http://127.0.0.1:${selectedPort}`;
      // spawn 前暴露真实端口，wait 期间监控打对地址；最多等待 60 秒，child 提前退出则立即失败。
      resolvedGatewayBaseUrl = portUrl;
      startLinuxPackagedGateway(selectedPort);
      if (await waitForHealthApi(portUrl, { process: managedGateway, generation, timeoutMs: 60_000 })) {
        console.log(`[gateway] Total ensureGateway time: ${Date.now() - ensureStart}ms`);
        return { baseUrl: portUrl, managed: true };
      }
      throw new Error('packaged Linux Crew Gateway exited before readiness');
    }

    // 3.5 macOS 打包版专属：自动寻找可用端口（8000~8009）启动二进制
    if (app.isPackaged && process.platform === 'darwin') {
      // 同 Windows：托管实例存活时复用，避免扫出新端口却 spawn 不出来空等。
      if (managedGateway) {
        console.log('[gateway] macOS packaged: managed instance alive, waiting on existing port...');
        logSupervisorDecision('reuse-alive-instance', { platform: 'darwin' });
        if (await waitForHealthApi(resolvedGatewayBaseUrl, { process: managedGateway, generation })) {
          return { baseUrl: resolvedGatewayBaseUrl, managed: true };
        }
      }
      let selectedPort = GATEWAY_PORT_MIN;
      for (let port = GATEWAY_PORT_MIN; port <= GATEWAY_PORT_MAX; port++) {
        const available = await isPortAvailable(port);
        if (available) {
          selectedPort = port;
          break;
        }
        if (port === GATEWAY_PORT_MAX) {
          console.error(`[gateway] All ports ${GATEWAY_PORT_MIN}-${GATEWAY_PORT_MAX} are in use, giving up`);
          throw new Error('no available port for the packaged Crew Gateway');
        }
        console.log(`[gateway] Port ${port} is in use, trying ${port + 1}...`);
      }
      const portUrl = `http://127.0.0.1:${selectedPort}`;
      // 与 Windows 一致：spawn 前暴露真实端口，wait 无冷启动天花板。
      resolvedGatewayBaseUrl = portUrl;
      startMacOSPackagedGateway(selectedPort);
      if (await waitForHealthApi(portUrl, { process: managedGateway, generation })) {
        return { baseUrl: portUrl, managed: true };
      }
      throw new Error('packaged macOS Crew Gateway exited before readiness');
    }

    // 4. 开发环境 / 兜底：拉起 28180 端口的 Python 子进程
    resolvedGatewayBaseUrl = MANAGED_GATEWAY_URL;
    startManagedGateway();
    if (await waitForHealthApi(MANAGED_GATEWAY_URL, { process: managedGateway, generation })) {
      return { baseUrl: MANAGED_GATEWAY_URL, managed: true };
    }

    throw new Error('Crew Gateway failed instance verification on every candidate port');
  })();
  ensureGatewayPromise = pending;
  void pending.catch(() => {
    // 只清自己这个 promise；retry 已接管（设置了新代际 promise）时不清。
    if (ensureGatewayPromise === pending) ensureGatewayPromise = null;
  });
  return pending;
}

async function securityGatewayRequest(
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE',
  pathname: string,
  payload?: Record<string, unknown>,
): Promise<{ ok: boolean; status: number; body: unknown }> {
  const usesRemoteAuth = usesGatewayRemoteAuth();
  const jwt = usesRemoteAuth ? loginNewServiceInstance.getJWTToken() : null;
  const identityHeaders = usesRemoteAuth ? gatewayIdentityHeaders() : null;
  if (usesRemoteAuth && (!jwt || !identityHeaders)) {
    return { ok: false, status: 401, body: { detail: '未登录' } };
  }
  const { baseUrl } = await ensureGateway();
  const body = payload === undefined ? '' : JSON.stringify(payload);
  const requestPath = new URL(pathname, 'http://127.0.0.1').pathname;
  const headers: Record<string, string> = {
    ...(body ? { 'content-type': 'application/json' } : {}),
    ...(usesRemoteAuth ? { Authorization: `Bearer ${jwt}`, ...identityHeaders } : {}),
    ...gatewayAccessHeaders(requestPath, method, body),
  };
  const response = await fetch(`${baseUrl}${pathname}`, {
    method,
    headers,
    ...(body ? { body } : {}),
  });
  const responseBody = await response.json().catch(() => ({ detail: response.statusText }));
  return { ok: response.ok, status: response.status, body: responseBody };
}

async function reportUpdateSecurityAlert(detail: string): Promise<void> {
  try {
    await securityGatewayRequest('POST', '/api/security/alerts/report', {
      kind: 'update_signature_failure',
      detail: detail.slice(0, 512),
    });
  } catch {
    // Alert reporting is best-effort; the update boundary itself already failed closed.
    console.warn('[update-security] alert-report-unavailable');
  }
}

function parseOrThrow<T>(parsed: { ok: true; value: T } | { ok: false; error: string }, channel: string): T {
  if (parsed.ok) return parsed.value;
  const err = new Error(`${IPC_ARG_VALIDATION_FAILED}: ${channel} ${parsed.error}`);
  console.warn(`[ipc] ${channel} rejected: ${parsed.error}`);
  throw err;
}

type TrustedIpcEvent = Electron.IpcMainEvent | Electron.IpcMainInvokeEvent;
type TrustedIpcHandler = (event: Electron.IpcMainInvokeEvent, ...args: unknown[]) => unknown;

function assertTrustedRenderer(event: TrustedIpcEvent): void {
  const senderFrame = event.senderFrame;
  if (!mainWindow || !senderFrame || event.sender.id !== mainWindow.webContents.id || senderFrame !== mainWindow.webContents.mainFrame) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: untrusted IPC sender`);
  }
  const expectedFile = path.join(__dirname, '../assets/index.html');
  if (!isTrustedRendererFileUrl(senderFrame.url, expectedFile)) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: IPC sender must be the packaged renderer`);
  }
}

function feedbackConsentContext(
  event: Electron.IpcMainInvokeEvent,
): FeedbackConsentContext {
  const senderFrame = event.senderFrame;
  if (!senderFrame) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: feedback sender frame unavailable`);
  }
  return {
    desktopSessionId: desktopFeedbackSessionId,
    origin: senderFrame.url,
    ownerId: currentBrowserOwnerId() ?? 'unauthenticated',
    webContentsId: event.sender.id,
  };
}

function feedbackConsentDetail(preview: {
  payload: { title: string; description: string };
  images: Array<{ name: string; bytes: number; digest: string }>;
}): string {
  const attachments = preview.images.length === 0
    ? '无图片附件'
    : preview.images.map((image, index) => (
      `${index + 1}. ${image.name} · ${image.bytes} bytes · SHA-256 ${image.digest.slice(0, 16)}…`
    )).join('\n');
  return [
    `标题：${preview.payload.title}`,
    '',
    '描述：',
    preview.payload.description,
    '',
    `附件（${preview.images.length}）：`,
    attachments,
    '',
    '以上是脱敏后的最终上传内容。本次同意仅可使用一次，并会很快过期。',
  ].join('\n');
}

function assertTrustedInspirationRenderer(event: TrustedIpcEvent): string {
  const senderFrame = event.senderFrame;
  const entry = Array.from(inspirationWindows.entries()).find(
    ([, win]) => !win.isDestroyed() && win.webContents.id === event.sender.id,
  );
  if (!entry || !senderFrame || senderFrame !== event.sender.mainFrame) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: untrusted inspiration IPC sender`);
  }
  let source: URL;
  try {
    source = new URL(senderFrame.url);
  } catch {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid inspiration IPC origin`);
  }
  if (
    source.protocol !== 'ace-site:'
    || source.hostname !== entry[0]
    || source.pathname !== '/'
    || source.username !== ''
    || source.password !== ''
    || source.port !== ''
    || source.search !== ''
    || source.hash !== ''
  ) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: untrusted inspiration IPC origin`);
  }
  return entry[0];
}

const MAX_TRUSTED_IPC_PAYLOAD_BYTES = 64 * 1024 * 1024;

function assertIpcPayloadSize(args: unknown[]): void {
  let size: number;
  try {
    size = serialize(args).byteLength;
  } catch {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: IPC payload is not serializable`);
  }
  if (size > MAX_TRUSTED_IPC_PAYLOAD_BYTES) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: IPC payload too large`);
  }
}

const rawIpcHandle = ipcMain.handle.bind(ipcMain);
const rawIpcOn = ipcMain.on.bind(ipcMain);

function trustedHandle(channel: IpcInvokeChannel, listener: TrustedIpcHandler): void {
  if (!isIpcInvokeChannel(channel)) {
    throw new Error(`refusing to register unallowlisted IPC channel: ${channel}`);
  }
  rawIpcHandle(channel, (event, ...args) => {
    assertTrustedRenderer(event);
    assertIpcPayloadSize(args);
    return listener(event, ...args);
  });
}

function trustedOn<T>(
  channel: IpcRendererToMainEventChannel,
  authenticate: (event: Electron.IpcMainEvent) => T,
  listener: (event: Electron.IpcMainEvent, identity: T, ...args: unknown[]) => void,
): void {
  if (!isIpcRendererToMainEventChannel(channel)) {
    throw new Error(`refusing to register unallowlisted IPC event channel: ${channel}`);
  }
  rawIpcOn(channel, (event, ...args) => {
    try {
      const identity = authenticate(event);
      assertIpcPayloadSize(args);
      listener(event, identity, ...args);
    } catch {
      console.warn(`[ipc] ${channel} rejected`);
    }
  });
}

function validateBrowserSessionId(raw: unknown): string {
  if (typeof raw !== 'string' || !/^[A-Za-z0-9_.:-]{1,200}$/.test(raw)) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid browser session id`);
  }
  return raw;
}

function validateBrowserPanelRequest(raw: unknown): {
  sessionId: string;
  tabLabel: string;
  mode: 'ai' | 'human' | 'paused';
  bounds: Electron.Rectangle;
  visible: boolean;
} {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid browser panel request`);
  }
  const value = raw as Record<string, unknown>;
  const sessionId = validateBrowserSessionId(value.sessionId);
  const tabLabel = typeof value.tabLabel === 'string' ? value.tabLabel : '';
  if (tabLabel.length > 200 || !/^(?:s[0-9a-f]{32}-[1-9]\d*|t[1-9]\d*)$/.test(tabLabel)) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid browser tab label`);
  }
  if (value.mode !== 'ai' && value.mode !== 'human' && value.mode !== 'paused') {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid browser control mode`);
  }
  if (!value.bounds || typeof value.bounds !== 'object' || Array.isArray(value.bounds)) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid browser panel bounds`);
  }
  const boundsValue = value.bounds as Record<string, unknown>;
  if (![boundsValue.x, boundsValue.y, boundsValue.width, boundsValue.height]
    .every((part) => typeof part === 'number')) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid browser panel bounds`);
  }
  const bounds = {
    x: boundsValue.x as number,
    y: boundsValue.y as number,
    width: boundsValue.width as number,
    height: boundsValue.height as number,
  };
  if (
    !Object.values(bounds).every(Number.isSafeInteger)
    || bounds.x < 0
    || bounds.y < 0
    || bounds.width < 0
    || bounds.height < 0
    || bounds.x > 16_384
    || bounds.y > 16_384
    || bounds.width > 16_384
    || bounds.height > 16_384
  ) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid browser panel bounds`);
  }
  if (typeof value.visible !== 'boolean') {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid browser panel visibility`);
  }
  return { sessionId, tabLabel, mode: value.mode, bounds, visible: value.visible };
}

function validateBrowserPanelIdentity(raw: unknown): { sessionId: string; tabLabel: string } {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid browser panel identity`);
  }
  const value = raw as Record<string, unknown>;
  const sessionId = validateBrowserSessionId(value.sessionId);
  const tabLabel = typeof value.tabLabel === 'string' ? value.tabLabel : '';
  if (tabLabel.length > 200 || !/^(?:s[0-9a-f]{32}-[1-9]\d*|t[1-9]\d*)$/.test(tabLabel)) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid browser tab label`);
  }
  return { sessionId, tabLabel };
}

function sendVersionUpdateDownloadProgress(payload: VersionUpdateDownloadProgressPayload): void {
  mainWindow?.webContents.send('version-update-download-progress', payload);
}

function updatesDir(): string {
  const userData = app.getPath('userData');
  fs.mkdirSync(userData, { recursive: true, mode: 0o700 });
  const directory = path.join(fs.realpathSync.native(userData), 'updates');
  ensurePrivateUpdateDirectory(directory);
  return directory;
}

/**
 * 启动时清理所有 `.part`：用户暂停后重启 / 下载卡死后退出重启 → 回初始态，
 * 旧 partial 一律丢弃（仅完整下载的包跨重启保留）。
 */
function sweepUpdatePartials(): void {
  try {
    const dir = updatesDir();
    if (!fs.existsSync(dir)) return;
    for (const name of fs.readdirSync(dir)) {
      if (name.endsWith('.part')) {
        try {
          removeManagedUpdateFile(path.join(dir, name));
        } catch {
          /* ignore individual file failures */
        }
      }
    }
  } catch (err) {
    console.warn('[update] sweep .part failed:', (err as Error).message);
  }
}

/**
 * 定期清理 updatesDir 旧包：保留当前已下载待安装的包 + 正在写入的 .part/目标，
 * 其余（历史版本残留）全部删除，避免长期占用磁盘。
 */
function cleanupOldPackages(): void {
  try {
    const dir = updatesDir();
    if (!fs.existsSync(dir)) return;
    const preserved = new Set<string>();
    const downloaded = readUpdateState().downloaded;
    if (downloaded) {
      preserved.add(path.resolve(downloaded.filePath));
      preserved.add(path.resolve(`${downloaded.filePath}.sig`));
    }
    for (const p of activeFilePaths()) preserved.add(path.resolve(p));
    for (const name of fs.readdirSync(dir)) {
      const full = path.resolve(path.join(dir, name));
      if (preserved.has(full)) continue;
      try {
        removeManagedUpdateFile(full);
      } catch {
        /* ignore */
      }
    }
  } catch (err) {
    console.warn('[update] cleanup old packages failed:', (err as Error).message);
  }
}

const UPDATE_CLEANUP_INTERVAL_MS = 24 * 60 * 60 * 1000; // 每日一次
let updateCleanupTimer: ReturnType<typeof setInterval> | null = null;

function startUpdateCleanupMonitor(): void {
  if (updateCleanupTimer) return;
  void cleanupOldPackages();
  updateCleanupTimer = setInterval(() => void cleanupOldPackages(), UPDATE_CLEANUP_INTERVAL_MS);
}

function stopUpdateCleanupMonitor(): void {
  if (updateCleanupTimer) {
    clearInterval(updateCleanupTimer);
    updateCleanupTimer = null;
  }
}

/**
 * 安装已下载的更新包（来源：update-state.downloaded，单一可信路径）。
 * 安装器模块保持已验证文件描述符，启动前再次复核路径身份，并只允许平台固定类型。
 */
let updateInstallInProgress = false;

async function installDownloadedUpdate(): Promise<VersionUpdatePackageResult> {
  if (updateInstallInProgress) {
    return { success: false, message: '更新安装已在进行中' };
  }
  const downloaded = readUpdateState().downloaded;
  if (!downloaded) {
    return { success: false, message: '没有已下载的更新包' };
  }
  updateInstallInProgress = true;
  const quitAfterLaunch = () => {
    setTimeout(() => {
      isQuitting = true;
      app.quit();
    }, 500);
  };

  sendVersionUpdateDownloadProgress({ phase: 'installing', percent: 100 });
  try {
    const result = await launchVerifiedDownloadedUpdate(
      downloaded,
      configuredUpdatePublicKey(),
    );
    if (result.consumesRecord) {
      try {
        setDownloadedRecord(null);
      } catch {
        console.warn('[update-security] installed-state-clear-rejected');
      }
    }
    sendVersionUpdateDownloadProgress({ phase: 'completed', percent: 100 });
    quitAfterLaunch();
    return { success: true, message: result.message };
  } catch (error) {
    updateInstallInProgress = false;
    const message = error instanceof Error
      ? error.message.slice(0, 300)
      : '更新已阻止：启动安装失败';
    if (/签名|校验|signature/i.test(message)) {
      void reportUpdateSecurityAlert(message);
    }
    sendVersionUpdateDownloadProgress({ phase: 'error', message });
    return { success: false, message };
  }
}

/**
 * 启动恢复：force 阻断锁仍在且本机版本未达标 → 立即推送 force 态（心跳前就盖阻断层）；
 * downloaded 仍存在 → 推送 downloaded 态供用户直接安装。已达标 / 文件丢失则清理状态。
 */
function restoreUpdateStateOnLaunch(): void {
  const state = readUpdateState();
  const localVersion = currentAppVersion(app);

  if (state.forceLock) {
    const decision = evaluateVersionUpdate(state.forceLock.requiredVersion, localVersion);
    if (decision.shouldProcess) {
      // 仍需强制更新：立即盖阻断层
      mainWindow?.webContents.send('version-update-available', {
        type: 'force',
        title: '提示',
        message: state.forceLock.message || '当前版本过低，请更新版本后使用。',
        version: state.forceLock.requiredVersion,
        reportedVersion: localVersion,
      });
    } else {
      // 本机版本已达标（上次更新成功）→ 解锁
      setForceLock(null);
    }
  }

  if (state.downloaded) {
    const stillNeeded = evaluateVersionUpdate(
      state.downloaded.version,
      localVersion,
    ).shouldProcess;
    const publicKey = configuredUpdatePublicKey();
    const verified = stillNeeded && publicKey
      ? verifyUpdateArtifact(
          state.downloaded.filePath,
          `${state.downloaded.filePath}.sig`,
          publicKey,
          state.downloaded.version,
          state.downloaded,
        )
      : { ok: false };
    if (!verified.ok) {
      if (publicKey) {
        void reportUpdateSecurityAlert('启动恢复时更新包签名校验失败');
      }
      try {
        setDownloadedRecord(null);
      } catch {
        console.warn('[update-security] stale-state-clear-rejected');
      }
    } else {
      sendVersionUpdateDownloadProgress({ phase: 'downloaded', percent: 100 });
    }
  }
}

const ALLOWED_MIME_TYPES = new Set([
  'image/png', 'image/jpeg', 'image/gif', 'image/webp',
  'application/pdf', 'text/plain', 'text/markdown',
]);

function mimeFromExt(ext: string): string {
  switch (ext) {
    case 'jpg':
    case 'jpeg': return 'image/jpeg';
    case 'png': return 'image/png';
    case 'gif': return 'image/gif';
    case 'webp': return 'image/webp';
    case 'bmp': return 'image/bmp';
    case 'ico': return 'image/x-icon';
    case 'tif':
    case 'tiff': return 'image/tiff';
    case 'pdf': return 'application/pdf';
    case 'docx': return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
    case 'pptx': return 'application/vnd.openxmlformats-officedocument.presentationml.presentation';
    case 'xlsx': return 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
    case 'txt': return 'text/plain';
    case 'md':
    case 'markdown': return 'text/markdown';
    default: return 'application/octet-stream';
  }
}

function registerIpc() {
  // Idempotency: registering handlers twice throws in Electron.
  if (ipcRegistered) {return;}
  ipcRegistered = true;

  trustedHandle('window:minimize', () => mainWindow?.minimize());
  trustedHandle('window:maximize', () => {
    if (!mainWindow) return;
    if (mainWindow.isMaximized()) mainWindow.unmaximize();
    else mainWindow.maximize();
  });
  trustedHandle('window:close', () => mainWindow?.close());
  trustedHandle('window:isMaximized', () => mainWindow?.isMaximized() ?? false);
  trustedHandle('inspiration:open-window', (_e, raw: unknown) => {
    const args = parseOrThrow(InspirationWindowArgs.parse(raw), 'inspiration:open-window');
    openInspirationWindow(args.inspirationId, args.title);
    return { ok: true, open: true };
  });
  trustedHandle('inspiration:close-window', (_e, raw: unknown) => {
    const args = parseOrThrow(InspirationWindowArgs.parse(raw), 'inspiration:close-window');
    closeInspirationWindow(args.inspirationId);
    return { ok: true, open: false };
  });
  trustedHandle('inspiration:window-state', (_e, raw: unknown) => {
    const args = parseOrThrow(InspirationWindowArgs.parse(raw), 'inspiration:window-state');
    const win = inspirationWindows.get(args.inspirationId);
    return { ok: true, open: Boolean(win && !win.isDestroyed()) };
  });
  // The guarded registration below preserves the legacy sticky-close main-event contract.
  trustedOn('inspiration:sticky-close', assertTrustedInspirationRenderer, (_event, inspirationId) => {
    closeInspirationWindow(inspirationId);
  });
  trustedHandle('app:quit', () => {
    isQuitting = true;
    app.quit();
  });
  trustedHandle('app:get-version', () => ({
    version: currentAppVersion(app),
    label: currentAppVersionLabel(app),
  }));
  trustedHandle('shell:openExternal', (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenExternalArgs.parse(raw), 'shell:openExternal');
    return shell.openExternal(args.url);
  });

  trustedHandle('shell:openPath', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'shell:openPath');
    const extraRoots: string[] = [];
    if (args.workspaceId) {
      const workspace = await resolveWorkspaceDirectoryInfo(
        args.workspaceId,
        () => securityGatewayRequest('GET', '/api/workspaces'),
      );
      if (!workspace.exists || !workspace.canonicalPath) {
        throw new Error(`${IPC_ARG_VALIDATION_FAILED}: Workspace directory is unavailable`);
      }
      extraRoots.push(workspace.canonicalPath);
    }
    const resolved = await resolveShellAllowedPath(args.path, extraRoots);
    return shell.openPath(resolved);
  });

  trustedHandle('dialog:saveLocalExport', async (_e, raw: unknown) => {
    const args = parseOrThrow(DialogSaveLocalExportArgs.parse(raw), 'dialog:saveLocalExport');
    const source = path.resolve(args.sourcePath);
    const ownerSegment = currentCrewFileOwnerSegment();
    if (!ownerSegment) throw new Error(`${IPC_ARG_VALIDATION_FAILED}: no active site owner`);
    const sitesRoot = path.resolve(activeGatewayCrewHome(), 'accounts', ownerSegment, 'sites');
    const relative = path.relative(sitesRoot, source);
    const segments = relative.split(path.sep);
    const validLocation = (
      relative !== ''
      && !relative.startsWith('..')
      && !path.isAbsolute(relative)
      && segments.length === 3
      && /^(?:site|canvas)_[0-9a-f]{12}$/i.test(segments[0] || '')
      && segments[1] === 'exports'
      && path.extname(segments[2] || '').toLowerCase() === '.zip'
    );
    if (!validLocation) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: export source must be the current owner's inspiration ZIP`);
    }
    const stat = await fs.promises.stat(source);
    if (!stat.isFile() || stat.nlink !== 1) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: export source is not a regular site ZIP`);
    }
    const realSource = await fs.promises.realpath(source);
    const realExportRoot = await fs.promises.realpath(path.dirname(source));
    if (path.dirname(realSource) !== realExportRoot || realSource !== source) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: export source path is not canonical`);
    }
    const result = await dialog.showSaveDialog(mainWindow!, {
      title: '分享灵感',
      defaultPath: path.join(app.getPath('downloads'), args.suggestedName),
      filters: [{ name: 'ZIP 压缩包', extensions: ['zip'] }],
    });
    if (result.canceled || !result.filePath) return { ok: false, canceled: true };
    await fs.promises.copyFile(source, result.filePath);
    return { ok: true, canceled: false, path: result.filePath };
  });

  trustedHandle('shell:readTextFile', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'shell:readTextFile');
    const resolved = await resolveShellAllowedPath(args.path);
    const stat = await fs.promises.stat(resolved);
    const maxBytes = 512 * 1024;
    if (!stat.isFile()) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:readTextFile target is not a file`);
    }
    if (stat.size > maxBytes) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:readTextFile file too large`);
    }
    return fs.promises.readFile(resolved, 'utf8');
  });

  trustedHandle('shell:writeTextFile', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellWriteTextFileArgs.parse(raw), 'shell:writeTextFile');
    const maxBytes = 2 * 1024 * 1024;
    const bytes = Buffer.byteLength(args.content, 'utf8');
    if (bytes > maxBytes) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:writeTextFile file too large`);
    }
    const resolved = authorizeOwnedRendererFile(args.path);
    await writeResolvedOwnedFile(resolved, args.content);
    return { ok: true };
  });

  trustedHandle('shell:readFileBase64', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'shell:readFileBase64');
    const resolved = authorizeOwnedRendererFile(args.path);
    const maxBytes = 50 * 1024 * 1024;
    if (resolved.identity.size > maxBytes) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:readFileBase64 file too large`);
    }
    const buffer = await readResolvedCrewFile(resolved);
    if (!buffer) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:readFileBase64 file is empty`);
    }
    return {
      base64: buffer.toString('base64'),
      mimeType: mimeFromExt(path.extname(resolved.filePath).slice(1).toLowerCase()),
    };
  });

  trustedHandle('shell:writeFileBase64', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellWriteFileBase64Args.parse(raw), 'shell:writeFileBase64');
    const buffer = Buffer.from(args.base64, 'base64');
    const maxBytes = 50 * 1024 * 1024;
    if (buffer.byteLength > maxBytes) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:writeFileBase64 file too large`);
    }
    const resolved = authorizeOwnedRendererFile(args.path);
    await writeResolvedOwnedFile(resolved, buffer);
    return { ok: true };
  });

  // 静默存在性探测：不抛 ENOENT。契约：仅当目标为普通文件时返回 true；
  // 目录 / 不存在 / 无权限路径 → false（文件改动卡只认文件 path）。
  trustedHandle('shell:pathExists', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'shell:pathExists');
    let resolved: string;
    try {
      resolved = await resolveShellAllowedPath(args.path);
    } catch {
      return false;
    }
    try {
      const stat = await fs.promises.stat(resolved);
      return stat.isFile();
    } catch {
      return false;
    }
  });

  // Renderer 只能提交 Workspace ID；root 来自已鉴权 Gateway 记录，避免任意路径枚举。
  trustedHandle('workspace:directoryInfo', async (_e, raw: unknown) => {
    const args = parseOrThrow(WorkspaceDirectoryArgs.parse(raw), 'workspace:directoryInfo');
    return resolveWorkspaceDirectoryInfo(
      args.workspaceId,
      () => securityGatewayRequest('GET', '/api/workspaces'),
    );
  });

  trustedHandle('shell:showItemInFolder', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'shell:showItemInFolder');
    const resolved = await resolveShellAllowedPath(args.path);
    shell.showItemInFolder(resolved);
  });

  trustedHandle('shell:listOpenApplications', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'shell:listOpenApplications');
    const resolved = authorizeOwnedRendererFile(args.path);
    return listOpenWithApplications(resolved.filePath);
  });

  trustedHandle('shell:openPathWith', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathWithArgs.parse(raw), 'shell:openPathWith');
    const resolved = authorizeOwnedRendererFile(args.path);
    await openFileWithApplication(resolved.filePath, args.applicationId);
    return { ok: true as const };
  });

  /**
   * wiki:openSourceFile — 用系统默认程序打开 Wiki 来源的原始文件。
   *
   * 渲染进程只传 sourceId/kbId（不可信）；主进程向 gateway 查询来源元数据拿到
   * original_path，realpath 校验必须落在 gateway 实际使用的 CREW_HOME 内
   * （wiki_lib/uploads 等都在其下；--dev 模式隔离在 userData/gateway-dev，
   * 与账号 home 不同），再 shell.openPath。渲染进程无法借此打开 CREW_HOME 外的任意文件。
   */
  trustedHandle('wiki:openSourceFile', async (_e, raw: unknown) => {
    const args = parseOrThrow(WikiOpenSourceFileArgs.parse(raw), 'wiki:openSourceFile');
    const ensured = await ensureGateway();
    const kbId = args.kbId || 'default';
    const listPath = `/api/wiki/sources?kb_id=${encodeURIComponent(kbId)}`;
    const res = await fetch(new URL(listPath, ensured.baseUrl).toString(), {
      headers: { ...gatewayAccessHeaders(listPath) },
    });
    if (!res.ok) {
      throw new Error(`查询 Wiki 来源失败（HTTP ${res.status}）`);
    }
    const payload = await res.json() as {
      sources?: Array<{ id?: string; original_path?: string | null }>;
    };
    const source = (payload.sources ?? []).find((item) => item.id === args.sourceId);
    const originalPath = String(source?.original_path ?? '').trim();
    if (!source || !originalPath) {
      throw new Error('找不到该来源的原始文件');
    }
    const crewHomeReal = await fs.promises.realpath(activeGatewayCrewHome());
    const real = await fs.promises.realpath(path.resolve(originalPath));
    if (real !== crewHomeReal && !real.startsWith(`${crewHomeReal}${path.sep}`)) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: wiki source file outside CREW_HOME`);
    }
    const stat = await fs.promises.stat(real);
    if (!stat.isFile()) {
      throw new Error('原始文件不是普通文件');
    }
    const openError = await shell.openPath(real);
    if (openError) {
      throw new Error(openError);
    }
    return { ok: true as const };
  });

  // Renderer never receives raw file bytes. The main process re-authorizes the
  // path against the current account's task/uploads roots, opens the authorized
  // inode without following a swapped leaf link, decodes it, then writes the
  // native bitmap to the OS clipboard.
  trustedHandle('clipboard:writeImage', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'clipboard:writeImage');
    const crewHome = activeGatewayCrewHome();
    const ownerSegment = currentCrewFileOwnerSegment();
    return writeOwnedImageToClipboard(
      args.path,
      crewHome,
      ownerSegment,
      undefined,
      () => (
        currentCrewFileOwnerSegment() === ownerSegment
        && activeGatewayCrewHome() === crewHome
      ),
    );
  });

  trustedHandle('image:showItemInFolder', (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'image:showItemInFolder');
    const resolved = authorizeOwnedImagePath(
      args.path,
      activeGatewayCrewHome(),
      currentCrewFileOwnerSegment(),
    );
    shell.showItemInFolder(resolved.filePath);
    return { ok: true };
  });

  trustedHandle('dialog:selectFile', async (event, raw: unknown) => {
    const args = parseOrThrow(DialogSelectFileArgs.parse(raw), 'dialog:selectFile');
    const properties: Array<'openFile' | 'multiSelections'> = args.multiSelect
      ? ['openFile', 'multiSelections']
      : ['openFile'];
    const r = await dialog.showOpenDialog(mainWindow!, {
      properties,
      filters: args.filters ?? [],
    });
    if (r.canceled || r.filePaths.length === 0) return null;

    const maxBytes = args.maxBytes ?? MAX_DIALOG_FILE_BYTES;

    if (args.returnType !== 'dataUrl' && args.returnType !== 'object') {
      return Promise.all(
        r.filePaths.map((filePath) =>
          selectedFileAuthority.authorize(event.sender.id, filePath, maxBytes)),
      );
    }

    const items = await Promise.all(
      r.filePaths.map(async (filePath) => {
        try {
          const canonicalPath = await selectedFileAuthority.authorize(
            event.sender.id,
            filePath,
            maxBytes,
          );
          const selected = await selectedFileAuthority.consume(
            event.sender.id,
            canonicalPath,
            maxBytes,
          );
          const ext = path.extname(canonicalPath).slice(1).toLowerCase();
          const mimeType = mimeFromExt(ext);
          if (!ALLOWED_MIME_TYPES.has(mimeType)) {
            return {
              path: canonicalPath,
              name: path.basename(canonicalPath),
              dataUrl: '',
              error: `MIME_NOT_ALLOWED: ${mimeType}`,
            };
          }
          return {
            path: canonicalPath,
            name: path.basename(canonicalPath),
            dataUrl: `data:${mimeType};base64,${selected.bytes.toString('base64')}`,
          };
      } catch (err) {
        const detail = err instanceof Error ? err.message : '';
        return {
          path: filePath,
          name: path.basename(filePath),
          dataUrl: '',
          error: detail.startsWith('FILE_TOO_LARGE')
            ? 'READ_FAILED: 文件超过大小上限'
            : 'READ_FAILED: 文件读取未通过安全校验',
        };
        }
      }),
    );
    return items;
  });

  trustedHandle('dialog:selectFolder', async (_e, raw: unknown) => {
    parseOrThrow(DialogSelectFolderArgs.parse(raw), 'dialog:selectFolder');
    const r = await dialog.showOpenDialog(mainWindow!, {
      properties: ['openDirectory', 'createDirectory'],
      title: '选择工作空间文件夹',
    });
    if (r.canceled || r.filePaths.length === 0) return null;
    return Promise.all(r.filePaths.map((folderPath) => fs.promises.realpath(folderPath)));
  });

  trustedHandle('app:get-auto-launch-enabled', () => {
    return { enabled: getAutoLaunchEnabled() };
  });
  trustedHandle('app:set-auto-launch-enabled', (_e, enabled: unknown) => {
    if (typeof enabled !== 'boolean') {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: app:set-auto-launch-enabled expected boolean`);
    }
    return { enabled: applyAutoLaunch(enabled) };
  });
  trustedHandle('app:get-close-behavior', () => {
    return loadDesktopPrefs();
  });
  trustedHandle('app:set-close-behavior', (_e, behavior: unknown) => {
    if (behavior !== 'tray' && behavior !== 'quit' && behavior !== 'ask') {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: app:set-close-behavior expected tray|quit|ask`);
    }
    return saveDesktopPrefs({ closeBehavior: behavior });
  });
  trustedHandle('app:get-system-locale', () => {
    return app.getLocale();
  });
  trustedHandle('app:renderer-initial-state-ready', () => {
    markRendererInitialStateReady();
    return { ok: true };
  });
  trustedHandle('security:get-strict-security', () => ({
    strictSecurityEnabled: isStrictSecurityEnabled(),
  }));
  trustedHandle('security:set-strict-security', async (_e, enabled: unknown) => {
    if (typeof enabled !== 'boolean') {
      throw new Error(
        `${IPC_ARG_VALIDATION_FAILED}: security:set-strict-security expected boolean`,
      );
    }
    if (!enabled) {
      throw new Error('strict security cannot be disabled');
    }
    const saved = saveStrictSecurityPreference(true);
    loginNewServiceInstance.setStrictSecurityEnabled(true);
    return saved;
  });

  trustedHandle('update:start-download', async (_e, raw: unknown) => {
    const args = parseOrThrow(UpdateStartDownloadArgs.parse(raw), 'update:start-download');
    const result = startUpdateDownload({ version: args.version, type: args.type, url: args.url });
    if (!result.success && /签名|校验|signature/i.test(result.message ?? '')) {
      void reportUpdateSecurityAlert(result.message ?? '更新包签名校验失败');
    }
    return result;
  });

  trustedHandle('update:pause', () => pauseUpdateDownload());
  trustedHandle('update:resume', () => resumeUpdateDownload());
  trustedHandle('update:retry', async (_e, raw: unknown) => {
    const args = parseOrThrow(UpdateStartDownloadArgs.parse(raw), 'update:retry');
    const result = retryUpdateDownload({ version: args.version, type: args.type, url: args.url });
    if (!result.success && /签名|校验|signature/i.test(result.message ?? '')) {
      void reportUpdateSecurityAlert(result.message ?? '更新包签名校验失败');
    }
    return result;
  });

  trustedHandle('update:install-package', async () => installDownloadedUpdate());

  trustedHandle('update:get-state', (): UpdateStateSnapshot => readUpdateState());

  trustedHandle('feedback:preview', async (event, raw: unknown) => {
    const args = parseOrThrow(FeedbackPreviewArgs.parse(raw), 'feedback:preview');
    const context = feedbackConsentContext(event);
    const preview = feedbackServiceInstance.createPreview(args, context);
    if (!preview.success) return preview;
    if (!mainWindow || mainWindow.isDestroyed()) {
      feedbackServiceInstance.cancelPreview(preview.previewId, context);
      return { success: false, canceled: true, message: '反馈确认窗口不可用' };
    }
    let decision: Electron.MessageBoxReturnValue;
    try {
      decision = await dialog.showMessageBox(mainWindow, {
        type: 'question',
        title: '确认提交反馈',
        message: '请预览并确认本次要上传的反馈',
        detail: feedbackConsentDetail(preview),
        buttons: ['取消', '同意并提交'],
        defaultId: 0,
        cancelId: 0,
        noLink: true,
      });
    } catch {
      feedbackServiceInstance.cancelPreview(preview.previewId, context);
      return { success: false, canceled: true, message: '反馈确认失败，未上传任何内容' };
    }
    if (decision.response !== 1) {
      feedbackServiceInstance.cancelPreview(preview.previewId, context);
      return { success: false, canceled: true, message: '已取消反馈提交' };
    }
    return feedbackServiceInstance.approvePreview(preview.previewId, context);
  });

  trustedHandle('feedback:submit', async (event, raw: unknown) => {
    const args = parseOrThrow(FeedbackSubmitArgs.parse(raw), 'feedback:submit');
    return feedbackServiceInstance.submitFeedback(args, feedbackConsentContext(event));
  });

  trustedHandle('feedback:cancel', (event, raw: unknown) => {
    const args = parseOrThrow(FeedbackCancelArgs.parse(raw), 'feedback:cancel');
    return feedbackServiceInstance.cancelFeedback(
      args.authority,
      feedbackConsentContext(event),
    );
  });

  trustedHandle('feedback:list', async (_e, raw: unknown) => {
    const args = parseOrThrow(FeedbackListArgs.parse(raw), 'feedback:list');
    return getFeedbackList(args);
  });

  trustedHandle('feedback:image', async (_e, raw: unknown) => {
    const args = parseOrThrow(FeedbackImageArgs.parse(raw), 'feedback:image');
    return getFeedbackImage(args.path);
  });

  trustedHandle('auth:heartbeat', async (_e, rawVersion: unknown) => {
    if (
      rawVersion !== undefined
      && (
        typeof rawVersion !== 'string'
        || rawVersion.length > 100
        || /[\r\n\0]/.test(rawVersion)
      )
    ) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: auth:heartbeat version must be string|undefined`);
    }
    try {
      const previousOwner = currentBrowserOwnerId();
      const { baseUrl } = await ensureGateway();
      await loginNewServiceInstance.refreshConfig(baseUrl);
      if (currentBrowserOwnerId() !== previousOwner) {
        feedbackServiceInstance.cancelAll();
      }
    } catch {
      // 心跳不阻塞 UI；失败时沿用旧会话态
    }
    pushSessionState();
    scheduleBrowserHostConnection();
    return { success: true };
  });

  trustedHandle('auth:get-state', async () => {
    const { baseUrl } = await ensureGateway();
    try {
      const previousOwner = currentBrowserOwnerId();
      const state = await loginNewServiceInstance.refreshConfig(baseUrl);
      if (currentBrowserOwnerId() !== previousOwner) {
        feedbackServiceInstance.cancelAll();
      }
      pushSessionState();
      scheduleBrowserHostConnection();
      return { ok: true, state };
    } catch {
      return {
        ok: false,
        error: '登录状态获取失败，请重试',
        state: loginNewServiceInstance.getState(),
      };
    }
  });

  trustedHandle('auth:send-code', async (_e, raw: unknown) => {
    const phoneNumber =
      typeof raw === 'object' && raw !== null
        ? String((raw as Record<string, unknown>).phoneNumber ?? '').trim()
        : '';
    if (!phoneNumber || phoneNumber.length > 32) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: auth:send-code phoneNumber required`);
    }
    const { baseUrl } = await ensureGateway();
    return loginNewServiceInstance.sendCode(baseUrl, phoneNumber);
  });

  trustedHandle('auth:login', async (_e, raw: unknown) => {
    const identifier =
      typeof raw === 'object' && raw !== null
        ? String((raw as Record<string, unknown>).identifier ?? '').trim()
        : '';
    const code =
      typeof raw === 'object' && raw !== null
        ? String((raw as Record<string, unknown>).code ?? '').trim()
        : '';
    if (!identifier || identifier.length > 128) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: auth:login identifier required`);
    }
    feedbackServiceInstance.cancelAll();
    const { baseUrl } = await ensureGateway();
    const authState = loginNewServiceInstance.getState();
    if (authState.mode === 'remote' && !code) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: auth:login code required`);
    }
    const result = authState.mode === 'email'
      ? await loginNewServiceInstance.loginWithEmail(baseUrl, identifier)
      : await loginNewServiceInstance.login(baseUrl, identifier, code);
    if (result.ok) {
      closeGatewaySockets('login-changed');
      await resetBrowserHost('login-changed');
      pushSessionState();
      scheduleBrowserHostConnection();
    }
    return result;
  });

  trustedHandle('auth:logout', async () => {
    feedbackServiceInstance.cancelAll();
    const { baseUrl } = await ensureGateway();
    const result = await loginNewServiceInstance.logout(baseUrl);
    if (result.ok) {
      closeGatewaySockets('logout');
      await resetBrowserHost('logout');
    }
    pushSessionState();
    return result;
  });

  trustedHandle('gateway:fetch', async (_e, raw: unknown) => {
    const args = parseOrThrow(GatewayFetchArgs.parse(raw), 'gateway:fetch');
    const usesRemoteAuth = usesGatewayRemoteAuth();
    const jwt = usesRemoteAuth ? loginNewServiceInstance.getJWTToken() : null;
    if (usesRemoteAuth && !jwt) {
      return {
        ok: false,
        status: 401,
        statusText: 'Unauthorized',
        body: JSON.stringify({ ok: false, error: '未登录' }),
        headers: { 'content-type': 'application/json' },
      };
    }
    const identityHeaders = usesRemoteAuth ? gatewayIdentityHeaders() : null;
    if (usesRemoteAuth && !identityHeaders) {
      return {
        ok: false,
        status: 401,
        statusText: 'Unauthorized',
        body: JSON.stringify({ ok: false, error: '登录信息缺失，请重新登录' }),
        headers: { 'content-type': 'application/json' },
      };
    }
    const ensured = await ensureGateway();
    const targetUrl = new URL(args.url);
    const ensuredUrl = new URL(ensured.baseUrl);
    targetUrl.protocol = ensuredUrl.protocol;
    targetUrl.hostname = ensuredUrl.hostname;
    targetUrl.port = ensuredUrl.port;
    const fetchInit: RequestInit = { method: args.init?.method || 'GET' };
    if (args.init?.headers) {
      // Denylist sensitive/request-smuggling headers from the renderer. The
      // URL is already clamped to the gateway host (no SSRF), but renderer-
      // supplied Cookie/Authorization/Host/Origin/Referer/Connection/
      // Content-Length must not pass through — the main process is the sole
      // token-setter for the gateway.
      const DENYLIST = new Set([
        'host', 'connection', 'cookie', 'authorization',
        'origin', 'referer', 'content-length', GATEWAY_INSTANCE_AUTH_HEADER.toLowerCase(),
      ]);
      const sanitized: Record<string, string> = {};
      const incoming = args.init.headers as Record<string, string>;
      for (const [key, value] of Object.entries(incoming)) {
        if (DENYLIST.has(String(key).toLowerCase())) continue;
        sanitized[key] = value;
      }
      fetchInit.headers = sanitized;
    }
    if (!usesRemoteAuth) {
      // 开发态：不带认证头，由后端 dev_mode loopback 放行。
      fetchInit.headers = { ...(fetchInit.headers as Record<string, string> | undefined) };
    } else {
      fetchInit.headers = {
        ...(fetchInit.headers as Record<string, string> | undefined),
        Authorization: `Bearer ${jwt}`,
        ...identityHeaders,
      };
    }
    if (args.init?.body !== undefined) fetchInit.body = args.init.body;
    const proofMethod = (args.init?.method || 'GET').toUpperCase();
    const proofPath = targetUrl.pathname;
    const proofBody = typeof args.init?.body === 'string' ? args.init.body : '';
    fetchInit.headers = {
      ...(fetchInit.headers as Record<string, string> | undefined),
      ...gatewayAccessHeaders(proofPath, proofMethod, proofBody),
    };
    const cuaAuthority = classifyCuaSetupAuthorityRequest(proofMethod, proofPath, proofBody);
    if (cuaAuthority === 'install') {
      const confirmed = await confirmCuaDriverInstall(mainWindow);
      if (!confirmed) {
        return {
          ok: false,
          status: 403,
          statusText: 'Forbidden',
          body: JSON.stringify({ ok: false, error: '用户取消了 CUA Driver 安装' }),
          headers: { 'content-type': 'application/json' },
        };
      }
    }
    const res = await fetch(targetUrl.toString(), fetchInit);
    const body = await res.text();
    return {
      ok: res.ok,
      status: res.status,
      statusText: res.statusText,
      body,
      headers: Object.fromEntries(res.headers.entries()),
    };
  });

  const gatewayStreamControllers = new Map<string, AbortController>();
  const GATEWAY_STREAM_IDLE_TIMEOUT_MS = 60 * 1000;
  const GATEWAY_STREAM_TOTAL_TIMEOUT_MS = 15 * 60 * 1000;
  const GATEWAY_STREAM_MAX_TOTAL_BYTES = 32 * 1024 * 1024;
  const GATEWAY_STREAM_MAX_CHUNK_BYTES = 256 * 1024;

  trustedHandle('gateway:stream-start', async (event, raw: unknown) => {
    const rawRequest = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {};
    const requestId = typeof rawRequest.request_id === 'string' ? rawRequest.request_id : '';
    if (!/^[A-Za-z0-9_.:-]{1,200}$/.test(requestId)) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid gateway stream request id`);
    }
    const args = parseOrThrow(GatewayFetchArgs.parse(raw), 'gateway:stream-start');
    const streamKey = `${event.sender.id}:${requestId}`;
    const send = (payload: Record<string, unknown>) => {
      if (!event.sender.isDestroyed()) {
        event.sender.send('gateway:stream-event', { request_id: requestId, ...payload });
      }
    };
    const usesRemoteAuth = usesGatewayRemoteAuth();
    const jwt = usesRemoteAuth ? loginNewServiceInstance.getJWTToken() : null;
    const identityHeaders = usesRemoteAuth ? gatewayIdentityHeaders() : null;
    if (usesRemoteAuth && (!jwt || !identityHeaders)) {
      send({ type: 'error', error: '未登录或登录用户缺少身份信息' });
      return { ok: false };
    }

    const ensured = await ensureGateway();
    const targetUrl = new URL(args.url);
    const ensuredUrl = new URL(ensured.baseUrl);
    targetUrl.protocol = ensuredUrl.protocol;
    targetUrl.hostname = ensuredUrl.hostname;
    targetUrl.port = ensuredUrl.port;
    const headers: Record<string, string> = {};
    const denylist = new Set([
      'host', 'connection', 'cookie', 'authorization',
      'origin', 'referer', 'content-length', GATEWAY_INSTANCE_AUTH_HEADER.toLowerCase(),
    ]);
    for (const [key, value] of Object.entries(args.init?.headers || {})) {
      if (!denylist.has(key.toLowerCase())) headers[key] = value;
    }
    if (usesRemoteAuth && jwt && identityHeaders) {
      headers.Authorization = `Bearer ${jwt}`;
      Object.assign(headers, identityHeaders);
    }
    const requestMethod = (args.init?.method || 'GET').toUpperCase();
    const requestBody = typeof args.init?.body === 'string' ? args.init.body : '';
    Object.assign(
      headers,
      gatewayAccessHeaders(targetUrl.pathname, requestMethod, requestBody),
    );
    const controller = new AbortController();
    const timeoutSignal = AbortSignal.timeout(GATEWAY_STREAM_TOTAL_TIMEOUT_MS);
    const requestSignal = AbortSignal.any([controller.signal, timeoutSignal]);
    gatewayStreamControllers.get(streamKey)?.abort();
    gatewayStreamControllers.set(streamKey, controller);
    try {
      const response = await fetch(targetUrl.toString(), {
        method: requestMethod,
        headers,
        ...(args.init?.body !== undefined ? { body: args.init.body } : {}),
        signal: requestSignal,
      });
      const responseHeaders = Object.fromEntries(
        Array.from(response.headers.entries()).filter(([key]) => (
          key === 'content-type' || key === 'cache-control' || key === 'x-accel-buffering'
        )),
      );
      send({
        type: 'head',
        status: response.status,
        headers: responseHeaders,
      });
      if (!response.body) {
        send({ type: 'end' });
        return { ok: response.ok };
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let totalBytes = 0;
      const readWithIdleTimeout = async () => {
        let timer: ReturnType<typeof setTimeout> | undefined;
        try {
          return await Promise.race([
            reader.read(),
            new Promise<ReadableStreamReadResult<Uint8Array>>((_, reject) => {
              timer = setTimeout(
                () => reject(new Error('gateway_stream_idle_timeout')),
                GATEWAY_STREAM_IDLE_TIMEOUT_MS,
              );
            }),
          ]);
        } finally {
          if (timer !== undefined) clearTimeout(timer);
        }
      };
      while (true) {
        const { done, value } = await readWithIdleTimeout();
        if (done) break;
        const text = decoder.decode(value, { stream: true });
        if (!text) continue;
        const chunkBytes = Buffer.byteLength(text, 'utf8');
        totalBytes += chunkBytes;
        if (
          chunkBytes > GATEWAY_STREAM_MAX_CHUNK_BYTES
          || totalBytes > GATEWAY_STREAM_MAX_TOTAL_BYTES
        ) {
          await reader.cancel();
          send({ type: 'error', error: 'Gateway 流响应超过安全上限' });
          return { ok: false };
        }
        send({ type: 'chunk', text });
      }
      const tail = decoder.decode();
      if (tail) {
        const tailBytes = Buffer.byteLength(tail, 'utf8');
        totalBytes += tailBytes;
        if (
          tailBytes > GATEWAY_STREAM_MAX_CHUNK_BYTES
          || totalBytes > GATEWAY_STREAM_MAX_TOTAL_BYTES
        ) {
          send({ type: 'error', error: 'Gateway 流响应超过安全上限' });
          return { ok: false };
        }
        send({ type: 'chunk', text: tail });
      }
      send({ type: 'end' });
      return { ok: response.ok };
    } catch (error) {
      if ((error as Error).name !== 'AbortError') {
        send({ type: 'error', error: 'Gateway 流请求失败' });
      }
      return { ok: false };
    } finally {
      if (gatewayStreamControllers.get(streamKey) === controller) {
        gatewayStreamControllers.delete(streamKey);
      }
    }
  });

  trustedHandle('gateway:stream-cancel', async (event, raw: unknown) => {
    const requestId = (
      raw && typeof raw === 'object' && typeof (raw as Record<string, unknown>).request_id === 'string'
        ? String((raw as Record<string, unknown>).request_id)
        : ''
    );
    if (!/^[A-Za-z0-9_.:-]{1,200}$/.test(requestId)) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid gateway stream request id`);
    }
    const streamKey = `${event.sender.id}:${requestId}`;
    gatewayStreamControllers.get(streamKey)?.abort();
    gatewayStreamControllers.delete(streamKey);
    return { ok: true };
  });

  /**
   * gateway:upload — 本地文件 multipart 上传通道（Wiki Phase 2）。
   *
   * gateway:fetch 只透传 string body，无法承载二进制；这里由 renderer 传文件
   * 绝对路径，主进程读文件 + Node 内置 FormData/Blob 组 multipart POST 到
   * gateway。目标 path 精确白名单（GATEWAY_UPLOAD_ALLOWED_PATHS，当前仅
   * /api/wiki/upload），hostname 钳制与 JWT/身份头注入与 gateway:fetch 一致。
   *
   * 返回 { results }：每个文件一项，shape 与 gateway:fetch 返回一致，
   * 本地失败（读不到/超限/非普通文件）合成为 4xx JSON 错误体。
   */
  trustedHandle('gateway:upload', async (event, raw: unknown) => {
    const args = parseOrThrow(GatewayUploadArgs.parse(raw), 'gateway:upload');

    interface UploadFileResult {
      path: string;
      ok: boolean;
      status: number;
      statusText: string;
      body: string;
      headers: Record<string, string>;
    }
    const localFailure = (filePath: string, status: number, message: string): UploadFileResult => ({
      path: filePath,
      ok: false,
      status,
      statusText: 'UPLOAD_LOCAL_FAILED',
      body: JSON.stringify({ ok: false, error: message }),
      headers: { 'content-type': 'application/json' },
    });
    // 认证级失败：与 gateway:fetch 返回同样的状态码/错误体，逐文件合成。
    const authFailure = (status: number, message: string) => ({
      results: args.files.map((f) => localFailure(f, status, message)),
    });
    const usesRemoteAuth = usesGatewayRemoteAuth();
    const jwt = usesRemoteAuth ? loginNewServiceInstance.getJWTToken() : null;
    if (usesRemoteAuth && !jwt) return authFailure(401, '未登录');
    const identityHeaders = usesRemoteAuth ? gatewayIdentityHeaders() : null;
    if (usesRemoteAuth && !identityHeaders) {
      return authFailure(401, '登录信息缺失，请重新登录');
    }

    const ensured = await ensureGateway();
    const targetUrl = new URL(args.url);
    const ensuredUrl = new URL(ensured.baseUrl);
    targetUrl.protocol = ensuredUrl.protocol;
    targetUrl.hostname = ensuredUrl.hostname;
    targetUrl.port = ensuredUrl.port;
    const uploadUrl = targetUrl.toString();
    // 与 gateway:fetch 相同：主进程是唯一 token-setter，renderer 不提供任何 header。
    const authHeaders: Record<string, string> = usesRemoteAuth
      ? { Authorization: `Bearer ${jwt}`, ...identityHeaders }
      : {};

    const results: UploadFileResult[] = [];
    for (const filePath of args.files) {
      let content: Buffer;
      let canonicalPath: string;
      try {
        const selected = await selectedFileAuthority.consume(
          event.sender.id,
          filePath,
          GATEWAY_UPLOAD_MAX_FILE_BYTES,
        );
        content = selected.bytes;
        canonicalPath = selected.canonicalPath;
        if (content.byteLength === 0) {
          results.push(localFailure(filePath, 400, '文件为空'));
          continue;
        }
      } catch (err) {
        const message = (err as Error).message;
        results.push(localFailure(
          filePath,
          message.includes('FILE_TOO_LARGE') ? 413 : 400,
          `读取文件失败：${message}`,
        ));
        continue;
      }
      // 后端 /api/wiki/upload 逐文件接收（字段名 file），一次请求一个文件。
      const form = new FormData();
      form.append('file', new Blob([new Uint8Array(content)]), path.basename(canonicalPath));
      // Materialize the exact multipart wire body before signing. This removes
      // the former UNSIGNED-PAYLOAD exception while preserving the boundary.
      const encoded = new Request(uploadUrl, { method: 'POST', body: form });
      const wireBody = Buffer.from(await encoded.arrayBuffer());
      const contentType = encoded.headers.get('content-type');
      if (!contentType) {
        results.push(localFailure(filePath, 500, '无法编码 multipart 请求'));
        continue;
      }
      try {
        const res = await fetch(uploadUrl, {
          method: 'POST',
          headers: {
            ...authHeaders,
            'Content-Type': contentType,
            ...gatewayAccessHeaders(targetUrl.pathname, 'POST', wireBody),
          },
          body: new Uint8Array(wireBody),
        });
        results.push({
          path: canonicalPath,
          ok: res.ok,
          status: res.status,
          statusText: res.statusText,
          body: await res.text(),
          headers: Object.fromEntries(res.headers.entries()),
        });
      } catch (err) {
        results.push(localFailure(filePath, 502, '上传请求失败：Gateway 未响应'));
      }
    }
    return { results };
  });

  trustedHandle('gateway:ensure', async () => ensureGateway());
  trustedHandle('gateway:get-status', () => backendStatusPayload(backendConnected));

  trustedHandle('security:pending', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityPendingArgs.parse(raw), 'security:pending');
    const query = new URLSearchParams({
      workspace_id: args.workspaceId,
      session_id: args.sessionId,
      ...(args.taskId ? { task_id: args.taskId } : {}),
    });
    const pathname = `/api/security/pending?${query.toString()}`;
    const result = await securityGatewayRequest('GET', pathname);
    if (!result.ok) return result;
    const body = result.body as { requests?: Array<Record<string, unknown>> };
    const requests = (body.requests ?? []).map((request) => {
      const requestId = typeof request['request_id'] === 'string' ? request['request_id'] : '';
      const nonce = typeof request['nonce'] === 'string' ? request['nonce'] : '';
      if (requestId && nonce) {
        securityApprovalAuthorities.set(requestId, {
          nonce,
          workspaceId: args.workspaceId,
          sessionId: args.sessionId,
          taskId: typeof request['task_id'] === 'string'
            ? request['task_id']
            : (args.taskId ?? ''),
          riskClass: typeof request['risk_class'] === 'string' ? request['risk_class'] : '',
        });
      }
      const { nonce: _nonce, ...safe } = request;
      void _nonce;
      return safe;
    });
    return { ...result, body: { requests } };
  });

  trustedHandle('security:set-mode', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityModeArgs.parse(raw), 'security:set-mode');
    if (args.mode === 'full_access' && !(await confirmFullAccessMode(mainWindow))) {
      return {
        ok: false,
        status: 409,
        body: { detail: '完全访问的主机级二次确认已取消' },
      };
    }
    let confirmationNonce: string | undefined;
    if (args.mode === 'full_access') {
      const challenge = await securityGatewayRequest(
        'GET',
        `/api/security/full-access-challenge?workspace_id=${encodeURIComponent(args.workspaceId)}&session_id=${encodeURIComponent(args.sessionId)}`,
      );
      if (!challenge.ok) return challenge;
      const nonce = (challenge.body as { nonce?: unknown })?.nonce;
      if (typeof nonce !== 'string' || !nonce) {
        return { ok: false, status: 409, body: { detail: '完全访问服务端确认不可用' } };
      }
      confirmationNonce = nonce;
    }
    return securityGatewayRequest('PUT', '/api/security/mode', {
      workspace_id: args.workspaceId,
      session_id: args.sessionId,
      mode: args.mode,
      ...(confirmationNonce ? { confirmation_nonce: confirmationNonce } : {}),
    });
  });

  trustedHandle('security:decide', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityDecisionArgs.parse(raw), 'security:decide');
    const authority = securityApprovalAuthorities.get(args.requestId);
    if (
      !authority
      || authority.workspaceId !== args.workspaceId
      || authority.sessionId !== args.sessionId
      || authority.taskId !== (args.taskId ?? '')
    ) {
      return { ok: false, status: 409, body: { detail: '批准请求已过期、未加载或上下文不匹配' } };
    }
    if (
      authority.riskClass === 'dangerous_command'
      && args.decision !== 'reject'
      && !(await confirmDangerousAction(mainWindow))
    ) {
      return { ok: false, status: 409, body: { detail: '高风险操作的主机级二次确认已取消' } };
    }
    const pathname = `/api/security/requests/${encodeURIComponent(args.requestId)}/decision`;
    const result = await securityGatewayRequest('POST', pathname, {
      workspace_id: args.workspaceId,
      session_id: args.sessionId,
      task_id: args.taskId ?? '',
      nonce: authority.nonce,
      decision: args.decision,
      ...(args.alwaysArgvPrefix ? { always_argv_prefix: args.alwaysArgvPrefix } : {}),
      ...(args.permissions ? { permissions: args.permissions } : {}),
    });
    // 仅成功才删 nonce：409 可能是瞬时（task_id 时空不一致等），若删掉会让后续所有点击
    // 都命中上面的"已过期或未加载"分支而无法重试。409 时保留 nonce，交给下一次 /pending
    // 轮询与渲染层错误处理去对账（请求真死了轮询不再返回，overlay 自然撤掉）。
    if (result.ok) securityApprovalAuthorities.delete(args.requestId);
    return result;
  });

  trustedHandle('security:capabilities', async () => securityGatewayRequest('GET', '/api/security/capabilities'));
  trustedHandle('security:rules', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityWorkspaceArgs.parse(raw), 'security:rules');
    return securityGatewayRequest('GET', `/api/security/rules?workspace_id=${encodeURIComponent(args.workspaceId)}`);
  });
  trustedHandle('security:set-rule', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityRuleMutationArgs.parse(raw), 'security:set-rule');
    return securityGatewayRequest('PATCH', `/api/security/rules/${encodeURIComponent(args.ruleId)}`, {
      workspace_id: args.workspaceId, enabled: args.enabled,
    });
  });
  trustedHandle('security:delete-rule', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityRuleMutationArgs.parse(raw), 'security:delete-rule');
    return securityGatewayRequest('DELETE', `/api/security/rules/${encodeURIComponent(args.ruleId)}?workspace_id=${encodeURIComponent(args.workspaceId)}`);
  });
  trustedHandle('security:audit', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityAuditArgs.parse(raw), 'security:audit');
    const query = new URLSearchParams({ limit: String(args.limit ?? 100), offset: String(args.offset ?? 0) });
    if (args.actionType) query.set('action_type', args.actionType);
    if (args.decision) query.set('decision', args.decision);
    if (args.sessionId) query.set('session_id', args.sessionId);
    if (args.workspaceId) query.set('workspace_id', args.workspaceId);
    if (args.taskId) query.set('task_id', args.taskId);
    if (args.startTime !== undefined) query.set('start_time', String(args.startTime));
    if (args.endTime !== undefined) query.set('end_time', String(args.endTime));
    query.set('sort', args.sort ?? 'newest');
    return securityGatewayRequest('GET', `/api/security/audit?${query.toString()}`);
  });
  trustedHandle('security:audit-export', async () => securityGatewayRequest('GET', '/api/security/audit/export'));
  trustedHandle('security:audit-purge', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityWorkspaceArgs.parse(raw), 'security:audit-purge');
    return securityGatewayRequest('POST', `/api/security/audit/purge-expired?workspace_id=${encodeURIComponent(args.workspaceId)}`);
  });
  trustedHandle('security:alerts', async () =>
    securityGatewayRequest('GET', '/api/security/alerts'));
  trustedHandle('security:alert-isolate', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityAlertActionArgs.parse(raw), 'security:alert-isolate');
    return securityGatewayRequest(
      'POST',
      `/api/security/alerts/${encodeURIComponent(args.alertId)}/isolate`,
    );
  });
  trustedHandle('security:alert-revoke', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityAlertActionArgs.parse(raw), 'security:alert-revoke');
    return securityGatewayRequest(
      'POST',
      `/api/security/alerts/${encodeURIComponent(args.alertId)}/revoke`,
    );
  });
  trustedHandle('security:alert-resolve', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityAlertActionArgs.parse(raw), 'security:alert-resolve');
    return securityGatewayRequest(
      'POST',
      `/api/security/alerts/${encodeURIComponent(args.alertId)}/resolve`,
    );
  });
  trustedHandle('security:uac-status', async () => {
    if (process.platform !== 'win32') return { enabled: true };
    return getWindowsUacStatus();
  });
  trustedHandle('security:enable-uac', async () => {
    if (process.platform !== 'win32') {
      return { ok: false, exitCode: null, detail: '当前设备无需手动启用此安全设置' };
    }
    return runElevatedUacEnable();
  });
  trustedHandle('security:setup', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecuritySetupArgs.parse(raw), 'security:setup');
    if (process.platform !== 'win32') {
      return { ok: false, exitCode: null, detail: '当前设备无需手动安装安全防护' };
    }
    const runtime = app.isPackaged
      ? path.join(process.resourcesPath, 'ace-security-runtime.exe')
      : String(process.env.ACE_SECURITY_RUNTIME ?? '').trim()
        || path.join(repoRoot(), 'security-runtime', 'bin', 'ace-security-runtime.exe');
    if (!path.isAbsolute(runtime) || !fs.existsSync(runtime) || path.basename(runtime) !== 'ace-security-runtime.exe') {
      return {
        ok: false,
        exitCode: null,
        detail: app.isPackaged
          ? '随包 runtime 缺失，请重装或修复'
          : '未找到 runtime：security-runtime/bin/ 下无预编译 exe，也未设 ACE_SECURITY_RUNTIME',
      };
    }
    return runElevatedSecuritySetup(runtime, path.join(app.getPath('userData'), 'security'), args.action);
  });
  // 冷启动/卡死时 renderer「重试」按钮调用。
  // 协作式作废（generation++）+ 等旧实例真正退出后才重建，保证同一时刻至多一个
  // ensureGateway 流程，从根源上消除「旧实例未退 → 扫到新端口 → spawn 被短路 →
  // 空等无进程端口」的循环拉起。外部/systemd gateway 无 managedGateway，直接重建。
  trustedHandle('gateway:retry', async () => {
    gatewayGeneration += 1;
    logSupervisorDecision('user-retry', { generation: gatewayGeneration });
    ensureGatewayPromise = null;
    await stopManagedGateway('user-retry');
    void ensureGateway()
      .then(() => scheduleBrowserHostConnection())
      .catch((err) => console.error('[gateway] retry failed:', err));
    return { ok: true };
  });

  trustedHandle('gateway-ws:connect', async (event) => {
    const senderId = event.sender.id;
    const previous = gatewaySockets.get(senderId);
    if (previous?.readyState === WebSocket.OPEN) {
      logMainStream('ws-connect-skip', { senderId, reason: 'already-open' });
      return { ok: true };
    }
    const generation = (gatewaySocketGenerations.get(senderId) ?? 0) + 1;
    gatewaySocketGenerations.set(senderId, generation);
    if (previous) {
      gatewaySockets.delete(senderId);
      try {
        previous.close(1000, 'reconnect');
      } catch {
        // best-effort reconnect cleanup
      }
    }

    const usesRemoteAuth = usesGatewayRemoteAuth();
    if (usesRemoteAuth && !loginNewServiceInstance.getJWTToken()) {
      return { ok: false, status: 401, error: '未登录' };
    }
    if (usesRemoteAuth && !gatewayIdentityHeaders()) {
      return { ok: false, status: 401, error: '登录信息缺失，请重新登录' };
    }
    const ensured = await ensureGateway();
    if (gatewaySocketGenerations.get(senderId) !== generation || event.sender.isDestroyed()) {
      return { ok: false, error: 'WebSocket 连接已取消' };
    }
    const jwt = usesRemoteAuth ? loginNewServiceInstance.getJWTToken() : null;
    const identityHeaders = usesRemoteAuth ? gatewayIdentityHeaders() : null;
    if (usesRemoteAuth && (!jwt || !identityHeaders)) {
      return { ok: false, status: 401, error: '登录状态已变更' };
    }
    const httpUrl = new URL(ensured.baseUrl);
    httpUrl.protocol = httpUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    httpUrl.pathname = '/ws';
    httpUrl.search = '';
    httpUrl.hash = '';

    const socket = new WebSocket(httpUrl.toString(), {
      headers: {
        ...(usesRemoteAuth ? { Authorization: `Bearer ${jwt}`, ...identityHeaders } : {}),
        ...gatewayAccessHeaders('/ws'),
      },
    });
    gatewaySockets.set(senderId, socket);
    gatewaySocketProtocolIdentities.set(socket, new GatewayWsProtocolIdentity());
    const sendEvent = (payload: Record<string, unknown>): void => {
      if (
        gatewaySocketGenerations.get(senderId) !== generation
        || gatewaySockets.get(senderId) !== socket
        || event.sender.isDestroyed()
      ) return;
      event.sender.send('gateway-ws:event', payload);
    };
    const handleRendererDestroyed = (): void => {
      if (gatewaySocketGenerations.get(senderId) === generation) {
        gatewaySocketGenerations.set(senderId, generation + 1);
      }
      if (gatewaySockets.get(senderId) === socket) gatewaySockets.delete(senderId);
      try {
        socket.close(1000, 'renderer-destroyed');
      } catch {
        // best-effort cleanup
      }
    };
    socket.on('open', () => {
      logMainStream('ws-open', { senderId, url: httpUrl.toString() });
      sendEvent({ type: 'open' });
    });
    socket.on('message', (data) => {
      let kind = 'unknown';
      let request_id: string | undefined;
      let session_id: string | undefined;
      try {
        const parsed = JSON.parse(data.toString()) as { kind?: string; request_id?: string; session_id?: string };
        kind = parsed.kind ?? 'unknown';
        request_id = parsed.request_id;
        session_id = parsed.session_id;
      } catch {
        /* ignore parse for log */
      }
      if (kind !== 'ping' && kind !== 'pong') {
        logMainStream('ws-message', { kind, request_id, session_id, senderId });
      }
      sendEvent({ type: 'message', data: data.toString() });
    });
    socket.on('close', (code, reason) => {
      event.sender.removeListener('destroyed', handleRendererDestroyed);
      logMainStream('ws-close', { code, reason: reason.toString(), senderId });
      sendEvent({
        type: 'close',
        code,
        reason: publicWebSocketCloseReason(reason),
      });
      if (gatewaySockets.get(senderId) === socket) gatewaySockets.delete(senderId);
    });
    socket.on('error', (err) => {
      void err;
      sendEvent({ type: 'error', error: 'WebSocket 连接失败' });
    });
    event.sender.once('destroyed', handleRendererDestroyed);
    return { ok: true };
  });

  trustedHandle('gateway-ws:send', (event, payload: unknown) => {
    const socket = gatewaySockets.get(event.sender.id);
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return { ok: false, error: 'WebSocket 未连接' };
    }
    const protocolIdentity = gatewaySocketProtocolIdentities.get(socket);
    if (
      !protocolIdentity
      || payload === null
      || typeof payload !== 'object'
      || Array.isArray(payload)
    ) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid gateway WebSocket payload`);
    }
    let data = '';
    try { data = protocolIdentity.encode(payload); } catch {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: gateway WebSocket payload must be serializable`);
    }
    if (Buffer.byteLength(data, 'utf8') > 4 * 1024 * 1024) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: gateway WebSocket payload too large`);
    }
    try {
      socket.send(data);
      return { ok: true };
    } catch {
      gatewaySockets.delete(event.sender.id);
      try { socket.close(4002, 'Protocol identity failed'); } catch { /* best effort */ }
      return { ok: false, error: 'WebSocket 发送失败' };
    }
  });

  trustedHandle('gateway-ws:close', (event) => {
    const senderId = event.sender.id;
    gatewaySocketGenerations.set(senderId, (gatewaySocketGenerations.get(senderId) ?? 0) + 1);
    const socket = gatewaySockets.get(senderId);
    if (socket) {
      gatewaySockets.delete(senderId);
      try { socket.close(1000, 'client-close'); } catch { /* best effort */ }
    }
    return { ok: true };
  });

  // Browser state/debug uses a separate authenticated socket. Page rendering
  // and human input stay inside the sandboxed WebContentsView owned by BrowserHost.
  trustedHandle('browser-ws:connect', async (event, rawSessionId: unknown) => {
    const sessionId = validateBrowserSessionId(rawSessionId);
    const senderId = event.sender.id;
    const generation = (browserSocketGenerations.get(senderId) ?? 0) + 1;
    browserSocketGenerations.set(senderId, generation);
    const previous = browserSockets.get(senderId);
    if (previous) {
      browserSockets.delete(senderId);
      try { previous.close(1000, 'switch-session'); } catch { /* best effort */ }
    }
    const usesRemoteAuth = usesGatewayRemoteAuth();
    if (usesRemoteAuth && (!loginNewServiceInstance.getJWTToken() || !gatewayIdentityHeaders())) {
      return { ok: false, status: 401, error: '未登录' };
    }
    const ensured = await ensureGateway();
    if (browserSocketGenerations.get(senderId) !== generation || event.sender.isDestroyed()) {
      return { ok: false, error: '浏览器状态连接已取消' };
    }
    const jwt = usesRemoteAuth ? loginNewServiceInstance.getJWTToken() : null;
    const identityHeaders = usesRemoteAuth ? gatewayIdentityHeaders() : null;
    if (usesRemoteAuth && (!jwt || !identityHeaders)) {
      return { ok: false, status: 401, error: '登录状态已变更' };
    }
    const target = new URL(ensured.baseUrl);
    target.protocol = target.protocol === 'https:' ? 'wss:' : 'ws:';
    target.pathname = `/ws/browser/${encodeURIComponent(sessionId)}`;
    target.search = '';
    target.hash = '';
    const socket = new WebSocket(target.toString(), {
      headers: gatewayAccessHeaders(target.pathname),
      maxPayload: 2 * 1024 * 1024,
    });
    browserSockets.set(senderId, socket);
    const sendEvent = (payload: Record<string, unknown>): void => {
      if (
        browserSocketGenerations.get(senderId) !== generation
        || browserSockets.get(senderId) !== socket
        || event.sender.isDestroyed()
      ) return;
      event.sender.send('browser-ws:event', payload);
    };
    const handleRendererDestroyed = (): void => {
      if (browserSocketGenerations.get(senderId) === generation) {
        browserSocketGenerations.set(senderId, generation + 1);
      }
      if (browserSockets.get(senderId) === socket) browserSockets.delete(senderId);
      try { socket.close(1000, 'renderer-destroyed'); } catch { /* best effort */ }
    };
    socket.on('open', () => sendEvent({ type: 'open', sessionId }));
    socket.on('message', (data) => sendEvent({ type: 'message', sessionId, data: data.toString() }));
    socket.on('close', (code, reason) => {
      event.sender.removeListener('destroyed', handleRendererDestroyed);
      if (browserSockets.get(senderId) !== socket) return;
      sendEvent({ type: 'close', sessionId, code, reason: publicWebSocketCloseReason(reason) });
      browserSockets.delete(senderId);
    });
    socket.on('error', () => sendEvent({ type: 'error', sessionId, error: '浏览器状态连接失败' }));
    event.sender.once('destroyed', handleRendererDestroyed);
    return { ok: true };
  });
  trustedHandle('browser-ws:close', (event) => {
    const senderId = event.sender.id;
    browserSocketGenerations.set(senderId, (browserSocketGenerations.get(senderId) ?? 0) + 1);
    const socket = browserSockets.get(senderId);
    if (socket) {
      browserSockets.delete(senderId);
      try { socket.close(1000, 'client-close'); } catch { /* best effort */ }
    }
    return { ok: true };
  });
  trustedHandle('browser-view:set-panel', (_event, raw: unknown) => {
    const request = validateBrowserPanelRequest(raw);
    const runtimeKey = currentBrowserRuntimeKey();
    if (!runtimeKey || !browserHost) return { ok: false, error: '桌面浏览器尚未连接' };
    try {
      browserHost.setPanel({ runtimeKey, ...request });
      return { ok: true };
      } catch (error) {
        return {
          ok: false,
          error: publicBrowserHostError(error),
        };
    }
  });
  trustedHandle('browser-view:hide', () => {
    browserHost?.hidePanel();
    return { ok: true };
  });
  trustedHandle('browser-view:get-navigation', (_event, raw: unknown) => {
    const request = validateBrowserPanelIdentity(raw);
    const runtimeKey = currentBrowserRuntimeKey();
    if (!runtimeKey || !browserHost) return { ok: false, error: '桌面浏览器尚未连接' };
    try {
      return {
        ok: true,
        navigation: browserHost.getPanelNavigation({ runtimeKey, ...request }),
      };
      } catch (error) {
        return {
          ok: false,
          error: publicBrowserHostError(error),
        };
    }
  });

  loginNewServiceInstance.setVersionUpdateHandler((payload) => {
    // force 策略：持久化阻断锁，保证重启后仍强制更新直到本机版本达标
    if (payload.type === 'force' && payload.version) {
      setForceLock({
        requiredVersion: payload.version,
        ...(payload.message ? { message: payload.message } : {}),
      });
    }
    mainWindow?.webContents.send('version-update-available', payload);
  });
  loginNewServiceInstance.setSessionExpiredHandler(async () => {
    closeGatewaySockets('session-expired');
    await resetBrowserHost('session-expired');
    pushSessionState();
  });
}

async function bootstrap() {
  await app.whenReady();
  loginNewServiceInstance.setStrictSecurityEnabled(isStrictSecurityEnabled());

  // 普通启动使用本地 Gateway 身份；只有显式 `--dev` 才使用隔离的开发身份。
  // 先于 crew-home / owner / 文件协议注册解析，确保下游各处读到一致的身份。
  const bootSession = loginNewServiceInstance.getSessionInfo();
  gatewayIdentityMode = resolveGatewayIdentityMode(
    IS_DEV_LAUNCH,
    loginNewServiceInstance.getJWTToken(),
    bootSession.userInfo,
  );
  if (gatewayIdentityMode === 'dev' && loginNewServiceInstance.getJWTToken()) {
    // 残留的持久化身份不能拥有 Gateway 数据：保留加密偏好，但不跑其心跳。
    loginNewServiceInstance.stopHeartbeat();
    loginNewServiceInstance.clearJWTToken();
  }
  console.log(`[gateway] identity mode: ${gatewayIdentityMode}`);

  registerCrewFileProtocol(activeGatewayCrewHome, currentCrewFileOwnerSegment);
  registerSitePreviewProtocol(async () => {
    const gateway = await ensureGateway();
    return { baseUrl: gateway.baseUrl, headers: gatewayAccessHeaders };
  });
  console.log(`[gateway] local identity mode: ${gatewayIdentityMode}`);

  // 🌟 优化：先创建窗口（显示 loading），Gateway 在后台启动
  // 避免串行等待导致用户长时间白屏，提升用户体验
  // Windows/macOS: 后台启动 Gateway，启动完成后推送 status 更新遮罩
  // Linux: 等待 wrapper 脚本拉起的 Gateway 就绪
  // 非打包态：同样后台启动 managed Gateway，避免旧 Gateway / browser-host
  // 连到 8000 端口的残留 Gateway 或空端口。
  if (IS_DEV_LAUNCH) {
    // 开发态统一使用托管 Gateway 端口，让浏览器客户端与 managed Gateway
    // 使用同一端口，避免默认 8000 上残留进程导致 403 / ECONNREFUSED。
    process.env.GATEWAY_PORT = String(MANAGED_GATEWAY_PORT);
  }

  // 🌟 关键优化：健康监控立即启动，不等 ensureGateway 完成
  // 这样渲染进程能实时收到 backend:status 推送，不再卡在 loading
  startBackendHealthMonitor();

  // 后台异步启动 Gateway（不阻塞窗口创建）
  ensureGateway()
    .then(result => {
      console.log('[main] Gateway started:', result);
      // 让浏览器客户端与 managed / 复用的 Gateway 使用同一端口，
      // 避免默认 8000 与实际端口不一致导致 WS 连不上。
      try {
        const resolvedPort = new URL(result.baseUrl).port;
        if (resolvedPort) {
          process.env.GATEWAY_PORT = resolvedPort;
        }
      } catch {
        /* ignore malformed URL */
      }
      scheduleBrowserHostConnection();
    })
    .catch(err => {
      console.error('[main] Gateway start failed:', err);
      // 进程可能仍在慢启动：保持健康监控，不在此处 push disconnected。
    });

  // 持久化恢复只做本地解密与 JWT 形状检查；随后由立即启动的 heartbeat 异步发现
  // 服务端明确返回的 unauthorized。网络不可用时当前产品语义仍允许离线恢复。
  // 不 await：bootstrap 不应被后台恢复阻塞，renderer 先以未登录状态启动。
  if (IS_DEV_LAUNCH) {
    // 开发启动已在 Gateway 拉起前恢复账号并固定本进程身份模式。
    pushSessionState();
  }
  registerIpc();
  createTray();
  createWindow();
  scheduleBrowserHostConnection();

  // 版本更新：配置下载控制器进度回调、清理上次中断的 .part、启动旧包定期清理；
  // 页面就绪后恢复 force 阻断 / 已下载待安装状态（renderer 监听器此时已绑定）。
  configureUpdateController(sendVersionUpdateDownloadProgress);
  sweepUpdatePartials();
  startUpdateCleanupMonitor();
  if (mainWindow) {
    mainWindow.webContents.once('did-finish-load', () => restoreUpdateStateOnLaunch());
  }

  // 注入卸载模块依赖（托盘「卸载」功能需要访问主进程内部状态）
  setUninstallDeps({
    getMainWindow: () => mainWindow,
    stopManagedGateway: (timeoutMs = 12000) => {
      return new Promise((resolve) => {
        if (!managedGateway) {
          clearManagedGatewayInstanceKey();
          return resolve();
        }
        const gw = managedGateway;
        const done = () => {
          if (managedGateway === gw) {
            managedGateway = null;
            clearManagedGatewayInstanceKey();
          }
          resolve();
        };
        gw.once('exit', done);
        const timer = setTimeout(() => {
          try { gw.kill('SIGKILL'); } catch { /* ignore */ }
          setTimeout(done, 500);
        }, timeoutMs);
        try { gw.stdin.end(); } catch {
          try { gw.kill(); } catch { /* already dead */ }
        }
        gw.once('exit', () => clearTimeout(timer));
      });
    },
    setQuittingFlag: () => { isQuitting = true; },
    resetQuittingFlag: () => { isQuitting = false; },
    stopBackendHealthMonitor,
    suppressBackendOverlay: () => {
      try {
        mainWindow?.webContents.send('backend:suppress-overlay');
      } catch {
        // webContents may be destroyed
      }
    },
  });

  app.on('activate', () => {
    // Hidden AutomationHost windows are implementation details and must not
    // suppress recreation of the user-facing window on macOS.
    if (!mainWindow || mainWindow.isDestroyed()) createWindow();
    else showMainWindow();
  });
}

app.on('window-all-closed', () => {
  loginNewServiceInstance.stopHeartbeat();
  if (process.platform !== 'darwin') app.quit();
});

// 优雅关闭：包含网络释放以及后台猎杀
app.on('before-quit', (event) => {
  isQuitting = true;
  feedbackServiceInstance.cancelAll();
  for (const win of inspirationWindows.values()) {
    if (!win.isDestroyed()) win.destroy();
  }
  inspirationWindows.clear();
  gatewayRestartController.stop();
  void resetBrowserHost('app-quit');
  closeGatewaySockets('app-quit');
  stopBackendHealthMonitor();
  stopUpdateCleanupMonitor();
  // 中止进行中的更新下载（.part 留盘，下次启动 sweep 清理；已下载完整包保留）
  try {
    disposeUpdateDownload();
  } catch (err) {
    console.warn('[main] disposeUpdateDownload failed:', (err as Error).message);
  }
  // Wait for the parent-liveness shutdown, then use stopManagedGateway's exact-child
  // timeout fallback before allowing Electron to exit.
  if (gatewayQuitCleanup) {
    event.preventDefault();
  } else if (managedGateway) {
    event.preventDefault();
    console.log('[main] Closing managed gateway before quit...');
    gatewayQuitCleanup = stopManagedGateway('app-quit')
      .catch((error) => {
        console.error('[main] managed gateway cleanup failed:', error);
      })
      .finally(() => {
        gatewayQuitCleanup = null;
        app.quit();
      });
  }
  if (!gatewayQuitCleanup) clearManagedGatewayInstanceKey();
});

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      showMainWindow();
    }
  });
  bootstrap().catch((err) => {
    console.error('Bootstrap failed:', err);
    app.quit();
  });
}
