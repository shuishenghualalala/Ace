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
import { app, BrowserWindow, ipcMain, shell, dialog, nativeImage, nativeTheme, protocol } from 'electron';
import * as path from 'path';
import * as fs from 'fs';
import * as net from 'net';
import { createHash, randomBytes } from 'crypto';
import { spawn, type ChildProcessWithoutNullStreams, type SpawnOptionsWithoutStdio } from 'child_process';
import { Readable, Transform } from 'stream';
import { pipeline } from 'stream/promises';
import type { ReadableStream as NodeReadableStream } from 'stream/web';
import WebSocket from 'ws';
import { BrowserHost, BrowserHostError } from './browser-host';
import { loginNewServiceInstance } from './auth-service';
import { submitFeedback, getFeedbackList, getFeedbackImage } from './feedback-service';
import {
  readResolvedCrewFile,
  registerCrewFileProtocol,
  resolveOwnedFilePath,
  type ResolvedOwnedFile,
} from './crew-file-protocol';
import { registerSitePreviewProtocol } from './site-preview-protocol';
import { installSitePreviewNavigation } from './site-preview-navigation';
import { createExternalLinkBridge } from './external-link-bridge';
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
  resolveTaskWorkspaceRoot,
} from './crew-session-file';
import { canonicalOwnedShellRoot, resolveAllowedShellPath } from './shell-allowed-path';
import {
  classifyCuaSetupAuthorityRequest,
  createDesktopSecurityProof,
  gatewayInstanceAccessToken,
  probeGatewayInstance,
  type GatewayComponentState,
  type GatewayInstanceProbe,
  type GatewayProbeFailureKind,
  type GatewayInstanceVerificationOptions,
} from './gateway-instance-auth';
import { probeBackendHealth } from './backend-health-probe';
import {
  GatewayReadinessTracker,
  GatewayStartupError,
  readinessFromMainProbe,
  type DesktopGatewayReadiness,
} from './gateway-readiness';
import { BackendHealthMonitor } from './backend-health-monitor';
import type {
  BackendHealthState,
  BackendProcessState,
  BackendTransportState,
} from './backend-health-monitor';
import { GatewayRestartController } from './gateway-restart-controller';
import { GatewayStartupLog } from './gateway-startup-log';
import { GatewayProcessRetirement, GatewayStopTimeoutError } from './gateway-process-retirement';
import { DesktopShutdown, waitForShutdownTask, type ShutdownReason } from './desktop-shutdown';
import { DesktopChatOutbox, desktopChatOutboxScope } from './chat-outbox';
import { MAX_CHAT_FRAME_BYTES, type DesktopChatOutboxEntry } from '../shared/chat-outbox';
import { RendererRecovery, type RecoveryEvent, type RendererFailure } from './renderer-recovery';
import { RendererCheckpointStore } from './renderer-checkpoint';
import { isTrustedRendererFileUrl } from './trusted-renderer-url';
import type {
  AuthStateSnapshot,
  VersionUpdateDownloadProgressPayload,
  VersionUpdatePackageResult,
  UpdateStateSnapshot,
  TrayStatus,
} from '../shared/types';
import {
  isFullAccessChoiceEnabled,
  isStrictSecurityEnabled,
  normalizeCloseBehavior,
  readDesktopPrefsFile,
  readSecurityPermissionPreferences,
  saveCloseBehaviorPreference,
  saveDefaultConversationPermissionPreference,
  saveFullAccessChoicePreference,
  type CloseBehavior,
} from './desktop-prefs';
import { logMainStream } from './stream-debug';
import { confirmCuaDriverInstall, confirmDefaultFullAccess } from './host-authority-dialog';
import {
  GatewayFetchArgs,
  GatewayUploadArgs,
  ShellOpenPathArgs,
  WorkspaceDirectoryArgs,
  ShellOpenPathWithArgs,
  ShellWriteFileBase64Args,
  ShellWriteTextFileArgs,
  FeedbackSubmitArgs,
  FeedbackListArgs,
  FeedbackImageArgs,
  DialogSelectFileArgs,
  DialogSelectFolderArgs,
  DialogSaveLocalExportArgs,
  TracingSaveExportArgs,
  InspirationWindowArgs,
  UpdateStartDownloadArgs,
  SecurityPendingArgs,
  SecurityDecisionArgs,
  SecurityDecisionReceiptArgs,
  SecurityModeArgs,
  SecurityWorkspaceArgs,
  SecurityRuleMutationArgs,
  SecurityAuditArgs,
  SecuritySetupArgs,
  WikiOpenSourceFileArgs,
} from '../shared/ipc-schemas';
import {
  createWindowsSandboxSetupCoordinator,
  getWindowsUacStatus,
  provisionInstalledWindowsSandbox,
  runElevatedUacEnable,
  securityRuntimePlatformKey,
} from './security-setup';
import {
  GATEWAY_UPLOAD_MAX_FILE_BYTES,
  IPC_ARG_VALIDATION_FAILED,
  MAX_DIALOG_FILE_BYTES,
} from '../shared/constants';
import { listOpenWithApplications, openFileWithApplication } from './open-with-service';
import { handleUninstall, setUninstallDeps } from './uninstall';
import { isTrayNotificationSummary, isTrayStatus, TrayService } from './tray-service';
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
  installableUpdateState,
  readUpdateState,
  setDownloadedRecord,
  setForceLock,
} from './update/update-state';
import {
  configuredUpdatePublicKey,
  verifyPackageSignature,
} from './update/update-integrity';
import {
  GITHUB_REPO,
  githubReleasesPageUrl,
  resolveGithubReleaseDownloadUrl,
  resolveGithubReleaseTarget,
} from './update/github-release';
import { launchUpdateInstaller } from './update/installer-handoff';
import { resolveWorkspaceDirectoryInfo } from './workspace-directory';
import { configurePptxWasmRuntime, PPTX_WASM_V8_FLAGS } from './wasm-runtime';
import { commitDownloadedExport } from './tracing-export-file';

// 必须早于 app.whenReady()/BrowserWindow 创建；该开关随同一安装包跨 Windows、macOS、Linux 生效。
configurePptxWasmRuntime(app.commandLine);

const DEFAULT_GATEWAY_URL = 'http://127.0.0.1:8000';
const GATEWAY_PORT_MIN = 8000;
const GATEWAY_PORT_RANGE_SIZE = 10;

// A private standard scheme lets the sandboxed Browser WebContents render one
// explicitly approved HTML artifact and its local static assets without file:// access.
protocol.registerSchemesAsPrivileged([
  {
    scheme: 'crew-artifact',
    privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true, corsEnabled: true },
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
let managedGateway: ChildProcessWithoutNullStreams | null = null;
interface GatewayBinding {
  baseUrl: string;
  managed: boolean;
  serverGeneration: string | null;
}
const gatewayLaunchKeys = new WeakMap<ChildProcessWithoutNullStreams, Buffer>();
const gatewayRetirement = new GatewayProcessRetirement({
  requestStop: (child) => { child.stdin?.end(); },
});
let ensureGatewayPromise: Promise<GatewayBinding> | null = null;
const securityApprovalNonces = new Map<string, string>();
const gatewaySockets = new Map<number, WebSocket>();
type GatewaySocketChat = { scope: string | null; capability?: { frame: string; storeId: string; requestStop: boolean } };
const gatewaySocketChat = new WeakMap<WebSocket, GatewaySocketChat>();
let desktopChatOutbox: DesktopChatOutbox | undefined;
let desktopRendererCheckpoint: RendererCheckpointStore | undefined;
let rendererSocketAttempt = 0;
const gatewaySocketGenerations = new Map<number, number>();
const browserSockets = new Map<number, WebSocket>();
const browserSocketGenerations = new Map<number, number>();
const gatewayStreamControllers = new Map<number, Map<string, AbortController>>();
let browserHost: BrowserHost | null = null;
let browserHostSocket: WebSocket | null = null;
let browserHostReconnectTimer: ReturnType<typeof setTimeout> | null = null;
let browserHostConnectionGeneration = 0;
let browserHostConnectPending = false;
let browserHostDisposePromise: Promise<void> | null = null;
let trayService: TrayService | null = null;

let rendererDocumentId = 0;
let nativeWindowReady = false;
let windowShowRequested = true;
let restoreMaximized = false;
let restoreMinimized = false;
let focusOnReveal = true;
let exhaustedRenderer: { documentId: number; failure: RendererFailure } | null = null;
let recoveryDialogOpen = false;
const rendererRecoveryEvents: Array<RecoveryEvent & { time: string }> = [];
const rendererRecovery = new RendererRecovery({
  record: recordRendererRecovery,
  release: (documentId) => {
    if (rendererDocumentId === documentId && mainWindow && !mainWindow.isDestroyed()) {
      releaseRendererDocument(mainWindow);
    }
  },
  restart: restartMainRenderer,
  exhausted: (documentId, failure) => {
    exhaustedRenderer = { documentId, failure };
    showRendererRecoveryDialog();
  },
  probeHeartbeat: (documentId) => {
    if (rendererDocumentId === documentId) sendToMainRenderer('renderer:heartbeat-probe');
  },
  watchHeartbeat: (documentId) => {
    const win = mainWindow;
    return !isQuitting && rendererDocumentId === documentId && win !== null && !win.isDestroyed()
      && win.isVisible() && !win.isMinimized() && win.isFocused();
  },
});

let isQuitting = false;
const GATEWAY_APP_QUIT_GRACE_MS = 33_000;
const desktopShutdown = new DesktopShutdown({
  prepare: prepareAppShutdown,
  exit: exitPreparedApp,
  resume: resumeAfterHandoffFailure,
  report: (error) => console.error('[shutdown] cleanup incomplete:', error),
});
// Gateway 重建代际：每次主动重试/重建递增。在途 ensureGateway 流程启动时记下
// 自己的代际，health wait 每轮校验——代际变了说明用户点了重试，立即中止让位，
// 避免「旧 wait 无超时挂着、新重建又被排队」的假死。
let gatewayGeneration = 0;
const gatewayReadiness = new GatewayReadinessTracker();
let gatewayAuthAbort = new AbortController();
let gatewayAuthRefresh: Promise<void> | null = null;
// A recovery of an owned instance must boot a replacement, even if an
// unrelated Gateway becomes available on the default port in the meantime.
let requireOwnedGateway = false;
// Backend health monitor state
let backendConnected = false;
let backendProcessState: BackendProcessState = 'starting';
let backendHealthState: BackendHealthState = 'unknown';
let backendTransportState: BackendTransportState = 'unknown';
// ensureGateway 缓存重证明的容错次数，与健康监控阈值语义独立。
const HEALTH_FAIL_THRESHOLD = 3;
let gatewayComponents: Record<string, GatewayComponentState> | undefined;
// Track the actually resolved gateway base URL (updated by ensureGateway)
let resolvedGatewayBaseUrl = DEFAULT_GATEWAY_URL;
// Idempotency guard for registerIpc().
// registerIpc() is called once at bootstrap, but defending against accidental
// re-invocation prevents duplicate ipcMain handlers (which would otherwise
// throw "attempt to register a second handler").
let ipcRegistered = false;
const externalLinkBridge = createExternalLinkBridge();

const gatewayRestartController = new GatewayRestartController(async () => {
  if (isQuitting) return;
  const generation = gatewayGeneration;
  try {
    if (!ensureGatewayPromise) {
      setBackendProcessState('restarting');
      logSupervisorDecision('automatic-restart', { generation });
    }
    await (ensureGatewayPromise ?? ensureGateway());
    assertGatewayGeneration(generation);
    scheduleBrowserHostConnection();
  } catch (error) {
    console.error('[gateway] automatic restart failed:', error);
    throw error;
  }
}, {
  // 用户重试作废旧 wait（GatewaySupersededError）不计失败，避免虚增退避指数。
  isIgnorableError: (error) => error instanceof GatewaySupersededError || error instanceof GatewayStartupError,
  // 熔断：连续 5 次短命/失败重启 → 停止自动拉起，置 error 终态等用户手动重试。
  stableUptimeMs: 60_000,
  maxConsecutiveFailures: 5,
  onTripped: ({ consecutiveFailures }) => {
    logSupervisorDecision('circuit-breaker-tripped', { consecutiveFailures });
    setBackendProcessState('error');
  },
});

function getTaskWorkspaceRoot(): string {
  const crewHome = activeGatewayCrewHome();
  const configPath = app.isPackaged
    ? path.join(crewHome, 'config.yaml')
    : path.join(repoRoot(), 'config', 'config.yaml');
  let configContents = '';
  try { configContents = fs.readFileSync(configPath, 'utf8'); } catch { /* use defaults */ }
  return resolveTaskWorkspaceRoot(crewHome, configContents);
}

/**
 * 校验并解析可在 shell:openPath / shell:readTextFile 中访问的绝对路径。
 *
 * @param extraRoots 调用方显式授权的额外根目录（如项目工作空间根），必须是绝对路径。
 */
function resolveShellAllowedPath(rawPath: string, extraRoots: string[] = []): string {
  const crewHome = activeGatewayCrewHome();
  const ownerSegment = currentCrewFileOwnerSegment();
  const taskWorkspaceRoot = getTaskWorkspaceRoot();
  // Crew Home also contains every account's .env and the Gateway instance key.
  // Only artifact roots belonging to the active account may be exposed here.
  const ownedRoots = ownerSegment ? [
    canonicalOwnedShellRoot(crewHome, ownerSegment, 'task_workspaces'),
    canonicalOwnedShellRoot(crewHome, ownerSegment, 'uploads'),
  ].filter((root): root is string => Boolean(root)) : [];
  const allowedRoots = [
    app.getPath('userData'),
    app.getPath('downloads'),
    app.getPath('documents'),
    app.getPath('pictures'),
    app.getPath('desktop'),
    taskWorkspaceRoot,
    ...ownedRoots,
    ...extraRoots,
  ];
  try {
    return resolveAllowedShellPath(rawPath, allowedRoots, {
      root: crewHome,
      allowedRoots: [taskWorkspaceRoot, ...ownedRoots],
    });
  } catch (error) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: ${(error as Error).message}`);
  }
}

const IS_DEV_LAUNCH = process.argv.includes('--dev');
const devGatewayPort = IS_DEV_LAUNCH ? Number(process.env['GATEWAY_PORT']) : NaN;
const MANAGED_GATEWAY_PORT = Number.isInteger(devGatewayPort) && devGatewayPort > 0 && devGatewayPort < 65535
  ? devGatewayPort
  : 28180;
const AUTOSTART_ARG = '--autostart';
const windowsSandboxSetup = createWindowsSandboxSetupCoordinator();
const RENDERER_LAUNCH_SEARCH = '';
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

function currentChatOutboxScope(): string | null {
  const owner = currentBrowserOwnerId();
  return owner ? desktopChatOutboxScope(gatewayIdentityMode, owner, activeGatewayCrewHome()) : null;
}

function chatOutbox(): DesktopChatOutbox {
  return desktopChatOutbox ??= new DesktopChatOutbox(path.join(app.getPath('userData'), 'chat-outbox'));
}

function rendererCheckpoint(): RendererCheckpointStore {
  return desktopRendererCheckpoint ??= new RendererCheckpointStore(path.join(app.getPath('userData'), 'renderer-checkpoint'));
}

function consumeSentCheckpoint(scope: string, entry: DesktopChatOutboxEntry): Promise<void> {
  return entry.phase === 'accepted' || entry.phase === 'queued'
    ? rendererCheckpoint().consume(scope, entry.value.composerCheckpoint)
    : Promise.resolve();
}

function requireChatOutboxScope(expected?: unknown): string {
  const scope = currentChatOutboxScope();
  if (!scope || (expected !== undefined && expected !== scope)) {
    throw new Error('Chat outbox account changed');
  }
  return scope;
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
    // silently dropped exact snapshots and made
    // large Playwright responses fail despite a healthy local connection.
    socket.send(payload);
    return true;
  } catch {
    return false;
  }
}

function ensureBrowserHost(): BrowserHost {
  if (isQuitting) throw new Error('Desktop is shutting down');
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
      headers: gatewayAccessHeaders('/api/browser/'),
      maxPayload: 0,
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
      void ensureBrowserHost().handleRpc(request, socket).then((result) => {
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
        const message = error instanceof Error ? error.message : '桌面浏览器操作失败';
        const sent = sendBrowserHostFrame(socket, {
          type: 'response',
          id,
          ok: false,
          error: message,
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
        const disposal = staleHost?.dispose() ?? Promise.resolve();
        browserHostDisposePromise = disposal;
        const finish = () => {
          if (browserHostDisposePromise === disposal) browserHostDisposePromise = null;
          if (
            generation !== browserHostConnectionGeneration
            || currentBrowserRuntimeKey() !== expectedRuntimeKey
            || isQuitting
          ) return;
          scheduleBrowserHostConnection(1500);
        };
        void disposal.then(finish, (error: unknown) => {
          console.error('[browser-host] cleanup after disconnect failed:', error);
          finish();
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
  if (!host) return pendingDisposal ?? undefined;
  const disposal = Promise.allSettled([pendingDisposal, host.dispose()]).then((results) => {
    const failed = results.find((result) => result.status === 'rejected');
    if (failed) throw failed.reason;
  });
  browserHostDisposePromise = disposal;
  try {
    await disposal;
  } finally {
    if (browserHostDisposePromise === disposal) browserHostDisposePromise = null;
  }
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
    fs.mkdirSync(logDir, { recursive: true });
    const logPath = path.join(logDir, 'main-crash.log');
    const ts = new Date().toISOString();
    const msg = `[${ts}] [${context}]\n${err instanceof Error ? (err.stack ?? err.message) : String(err)}\n\n`;
    fs.appendFileSync(logPath, msg, 'utf8');
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

// ── 渲染层错误上报落盘（renderer:report-error，preload 单向 send）──────────
// 渲染层的 window.onerror / unhandledrejection / 渲染隔离兜底统一落
// userData/logs/renderer-errors.log；单文件 5MB 轮转保留 1 份历史（.1）。
const RENDERER_ERROR_LOG_MAX_BYTES = 5 * 1024 * 1024;

function rendererErrorLogPath(): string {
  return path.join(app.getPath('userData'), 'logs', 'renderer-errors.log');
}

function appendRendererErrorLog(report: unknown): void {
  try {
    const r = report as { source?: unknown; message?: unknown; stack?: unknown; context?: unknown };
    if (typeof r?.message !== 'string') return;
    const file = rendererErrorLogPath();
    fs.mkdirSync(path.dirname(file), { recursive: true });
    try {
      if (fs.statSync(file).size >= RENDERER_ERROR_LOG_MAX_BYTES) {
        try { fs.rmSync(`${file}.1`, { force: true }); } catch { /* best-effort */ }
        fs.renameSync(file, `${file}.1`);
      }
    } catch {
      /* 尚无旧文件 */
    }
    const source = typeof r.source === 'string' ? r.source : 'unknown';
    const context = r.context ? ` ${JSON.stringify(r.context)}` : '';
    const stack = typeof r.stack === 'string' ? `\n${r.stack}` : '';
    fs.appendFileSync(file, `[${new Date().toISOString()}] [${source}] ${r.message}${stack}${context}\n`, 'utf8');
  } catch {
    /* 日志失败不阻断 */
  }
}

ipcMain.on('renderer:report-error', (_event, report) => appendRendererErrorLog(report));

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

function recordRendererRecovery(event: RecoveryEvent): void {
  const record = { ...event, time: new Date().toISOString() };
  rendererRecoveryEvents.push(record);
  if (rendererRecoveryEvents.length > 96) rendererRecoveryEvents.shift();
  console.log('[renderer-recovery]', JSON.stringify(record));
  try {
    const file = path.join(app.getPath('userData'), 'logs', 'renderer-recovery.jsonl');
    fs.mkdirSync(path.dirname(file), { recursive: true });
    if (fs.existsSync(file) && fs.statSync(file).size > 128 * 1024) fs.renameSync(file, `${file}.1`);
    fs.appendFileSync(file, `${JSON.stringify(record)}\n`, { encoding: 'utf8', mode: 0o600 });
  } catch {
    // The in-memory events are still available when diagnostics are exported.
  }
}

function sendToMainRenderer(channel: string, ...args: unknown[]): void {
  if (!mainWindow || mainWindow.isDestroyed() || !rendererRecovery.canServe(rendererDocumentId)) return;
  const contents = mainWindow.webContents;
  if (contents.isDestroyed() || contents.isCrashed()) return;
  try { contents.send(channel, ...args); } catch {
    // The native frame can detach while a replacement is loading.
  }
}

function releaseRendererDocument(win: BrowserWindow): void {
  releaseRendererTransports(win.webContents.id);
  browserHost?.hidePanel();
  setBackendTransportState('disconnected');
}

function restartMainRenderer(documentId: number): void {
  if (isQuitting || rendererDocumentId !== documentId || !mainWindow || mainWindow.isDestroyed()) return;
  try {
    createWindow(mainWindow);
  } catch {
    rendererRecovery.replacementFailed(documentId);
  }
}

function showNativeMainWindow(win: BrowserWindow, activate: boolean): void {
  win.setSkipTaskbar(false);
  if (win.isVisible()) return;
  const show = () => activate ? win.show() : win.showInactive();
  show();
  // A Windows launcher can supply SW_HIDE for the first native show call.
  if (process.platform === 'win32' && !win.isVisible()) show();
}

function showRendererRecoveryDialog(): void {
  const exhausted = exhaustedRenderer;
  if (!exhausted || !windowShowRequested || isQuitting || recoveryDialogOpen) return;
  recoveryDialogOpen = true;
  void (async () => {
    while (exhaustedRenderer === exhausted && windowShowRequested && !isQuitting) {
      if (mainWindow && !mainWindow.isDestroyed()) showNativeMainWindow(mainWindow, true);
      const { response } = await dialog.showMessageBox({
        type: 'error', title: 'Ace 无法打开窗口', message: 'Ace 窗口多次恢复失败',
        detail: `你可以重试打开窗口，或保存本地诊断。故障类型 ${exhausted.failure.reason}`,
        buttons: ['重试', '导出诊断', '退出'], defaultId: 0, cancelId: 2, noLink: true,
      });
      if (isQuitting || exhaustedRenderer !== exhausted) return;
      if (response === 0) {
        exhaustedRenderer = null;
        rendererRecovery.retry(exhausted.documentId);
        return;
      }
      if (response !== 1) {
        app.quit();
        return;
      }
      const chosen = await dialog.showSaveDialog({
        title: '保存窗口恢复诊断', defaultPath: 'ace-renderer-recovery.json',
        filters: [{ name: 'JSON', extensions: ['json'] }],
      });
      if (!chosen.canceled && chosen.filePath && !isQuitting && exhaustedRenderer === exhausted) {
        try {
          await fs.promises.writeFile(chosen.filePath, JSON.stringify({
            version: app.getVersion(), platform: process.platform, arch: process.arch,
            electron: process.versions.electron, events: rendererRecoveryEvents,
          }, null, 2), 'utf8');
        } catch {
          await dialog.showMessageBox({ type: 'warning', message: '无法保存窗口恢复诊断' });
        }
      }
    }
  })().catch(() => {
    console.warn('[renderer-recovery] native recovery dialog unavailable');
  }).finally(() => { recoveryDialogOpen = false; });
}

function revealMainWindowIfReady(): void {
  const win = mainWindow;
  if (isQuitting || !win || win.isDestroyed() || !nativeWindowReady || !windowShowRequested
    || !rendererRecovery.isReady(rendererDocumentId)) return;
  if (focusOnReveal && win.isMinimized()) win.restore();
  showNativeMainWindow(win, focusOnReveal);
  if (restoreMaximized) { restoreMaximized = false; win.maximize(); }
  if (restoreMinimized) { restoreMinimized = false; win.minimize(); }
  else if (focusOnReveal) win.focus();
  focusOnReveal = false;
}

function showMainWindow(): void {
  if (isQuitting) return;
  windowShowRequested = true;
  focusOnReveal = true;
  restoreMinimized = false;
  if (exhaustedRenderer) showRendererRecoveryDialog();
  else revealMainWindowIfReady();
}

function hideMainWindowToTray(): void {
  windowShowRequested = false;
  if (!mainWindow || mainWindow.isDestroyed()) return;
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
  if (trayService) return;
  trayService = new TrayService({
    assetsDir: path.join(__dirname, '../assets'),
    onActivate: () => {
      showMainWindow();
      sendToMainRenderer('tray:activated');
    },
    onUninstall: () => { if (!isQuitting) void handleUninstall(); },
    onQuit: () => app.quit(),
    onNotificationSelected: (id: string) => {
      showMainWindow();
      sendToMainRenderer('tray:notification-selected', id);
    },
    onNotificationsMarkAllRead: () => {
      showMainWindow();
      sendToMainRenderer('tray:notifications-mark-all-read');
    },
  });
  trayService.create();
}

function setTrayStatus(status: TrayStatus): void {
  trayService?.setStatus(status);
}

function rendererAuthState(): AuthStateSnapshot {
  if (usesDevGatewayIdentity()) {
    return {
      mode: 'dev',
      configured: true,
      providerId: 'dev',
      isLoggedIn: true,
      user: { userId: 'dev', phoneNumber: '', displayName: '开发者' },
    };
  }
  return loginNewServiceInstance.getState();
}

let publishedSessionOwner: string | null = null;
let authRetryTimer: ReturnType<typeof setTimeout> | null = null;
let authRetryDelay = 2_000;

function clearAuthRetry(): void {
  if (authRetryTimer) clearTimeout(authRetryTimer);
  authRetryTimer = null;
  authRetryDelay = 2_000;
}

function scheduleAuthVerification(): void {
  if (isQuitting || !rendererAuthState().verificationPending) {
    clearAuthRetry();
    return;
  }
  if (authRetryTimer) return;
  const generation = gatewayGeneration;
  authRetryTimer = setTimeout(() => {
    authRetryTimer = null;
    if (isQuitting || generation !== gatewayGeneration || !rendererAuthState().verificationPending) return;
    authRetryDelay = Math.min(authRetryDelay * 2, 30_000);
    // Re-prove the endpoint before supplying a saved identity to a new request.
    void ensureGateway().then(async ({ baseUrl }) => {
      if (!isQuitting && generation === gatewayGeneration) await refreshGatewayAuth(baseUrl);
    }).catch(() => {}).finally(() => {
      if (generation === gatewayGeneration) scheduleAuthVerification();
    });
  }, authRetryDelay);
}

function pushSessionState(resetReason?: string): Promise<void> {
  const owner = currentBrowserOwnerId();
  const reason = resetReason ?? (publishedSessionOwner && publishedSessionOwner !== owner ? 'auth-state-changed' : null);
  publishedSessionOwner = owner;
  let cleanup = Promise.resolve();
  if (reason) {
    closeGatewaySockets(reason);
    cleanup = resetBrowserHost(reason).catch((error: unknown) => console.warn('[browser-host]', error));
  }
  sendToMainRenderer('auth:session-state', rendererAuthState());
  scheduleAuthVerification();
  return cleanup;
}

function refreshGatewayAuth(baseUrl: string): Promise<void> {
  if (usesDevGatewayIdentity()) return Promise.resolve();
  if (gatewayAuthRefresh) return gatewayAuthRefresh;
  const signal = AbortSignal.any([gatewayAuthAbort.signal, AbortSignal.timeout(8_000)]);
  const refresh = loginNewServiceInstance.refreshConfig(baseUrl, signal).then(() => {
    if (isQuitting || signal.aborted) return;
    pushSessionState();
    scheduleBrowserHostConnection();
  }).finally(() => {
    if (gatewayAuthRefresh === refresh) gatewayAuthRefresh = null;
  });
  gatewayAuthRefresh = refresh;
  return refresh;
}

function resetGatewayAuthRefresh(): void {
  clearAuthRetry();
  gatewayAuthAbort.abort();
  gatewayAuthAbort = new AbortController();
  gatewayAuthRefresh = null;
}

function releaseRendererSockets(senderId: number, reason: string): WebSocket[] {
  const closing: WebSocket[] = [];
  for (const [sockets, generations] of [
    [gatewaySockets, gatewaySocketGenerations],
    [browserSockets, browserSocketGenerations],
  ] as const) {
    generations.delete(senderId);
    const socket = sockets.get(senderId);
    if (!socket) continue;
    sockets.delete(senderId);
    closing.push(socket);
    try { socket.close(1000, reason); } catch { /* best-effort disconnect */ }
  }
  return closing;
}

function releaseRendererTransports(senderId: number): void {
  releaseRendererSockets(senderId, 'renderer-document-ended');
  const streams = gatewayStreamControllers.get(senderId);
  gatewayStreamControllers.delete(senderId);
  if (streams) {
    for (const controller of streams.values()) controller.abort();
  }
}

function closeGatewaySockets(reason = 'auth-state-changed'): WebSocket[] {
  const senders = new Set([...gatewaySocketGenerations.keys(), ...browserSocketGenerations.keys()]);
  return Array.from(senders).flatMap((senderId) => releaseRendererSockets(senderId, reason));
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
  installSitePreviewNavigation(win.webContents, externalLinkBridge.openUrl);
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

function createWindow(previous?: BrowserWindow): void {
  const bounds = previous?.getNormalBounds() ?? { width: 1280, height: 800 };
  const maximize = previous ? restoreMaximized || previous.isMaximized() : false;
  const minimize = previous ? restoreMinimized || previous.isMinimized() : false;
  const focus = previous ? focusOnReveal || previous.isFocused() : true;
  if (!previous) windowShowRequested = !shouldLaunchHidden();
  const win = new BrowserWindow({
    ...bounds,
    minWidth: 960,
    minHeight: 640,
    resizable: true,
    frame: false,
    icon: resolveAppIcon(),
    backgroundColor: resolveWindowBackgroundColor(),
    title: 'Crew Desktop',
    show: false,
    skipTaskbar: !windowShowRequested,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      webviewTag: false,
      // Electron 36+ does not forward dynamically appended switches to renderers.
      additionalArguments: [`--js-flags=${PPTX_WASM_V8_FLAGS.join(' ')}`],
    },
  });
  mainWindow = win;
  nativeWindowReady = false;
  restoreMaximized = maximize;
  restoreMinimized = minimize;
  focusOnReveal = focus;
  exhaustedRenderer = null;
  const contents = win.webContents;
  const senderId = contents.id;
  const expected = path.join(__dirname, '../assets/index.html');
  let startedNavigation = false;
  let documentId = rendererRecovery.begin();
  rendererDocumentId = documentId;
  const currentWindow = () => mainWindow === win && !win.isDestroyed() && !isQuitting;
  if (!browserHostDisposePromise) ensureBrowserHost();

  installSitePreviewNavigation(contents, externalLinkBridge.openUrl);
  contents.on('will-navigate', (event, url) => {
    if (!isTrustedRendererFileUrl(url, expected, RENDERER_LAUNCH_SEARCH)) event.preventDefault();
  });
  win.once('ready-to-show', () => {
    if (!currentWindow()) return;
    nativeWindowReady = true;
    revealMainWindowIfReady();
  });
  contents.on('render-process-gone', (_event, details) => {
    if (currentWindow()) rendererRecovery.fail(documentId, { reason: details.reason, code: details.exitCode });
  });
  contents.on('unresponsive', () => {
    if (currentWindow()) rendererRecovery.unresponsive(documentId);
  });
  contents.on('responsive', () => {
    if (currentWindow()) rendererRecovery.responsive(documentId);
  });
  contents.on('did-fail-load', (_event, code, _description, _url, isMainFrame) => {
    if (currentWindow() && isMainFrame && code !== -3) {
      rendererRecovery.fail(documentId, { reason: 'load-failed', code });
    }
  });
  contents.on('did-start-navigation', (_event, url, isInPlace, isMainFrame) => {
    if (!currentWindow() || !isMainFrame || isInPlace || !isTrustedRendererFileUrl(url, expected, RENDERER_LAUNCH_SEARCH)) return;
    if (startedNavigation) {
      releaseRendererDocument(win);
      exhaustedRenderer = null;
      documentId = rendererRecovery.begin();
      rendererDocumentId = documentId;
    }
    startedNavigation = true;
  });
  contents.on('did-finish-load', () => {
    if (!currentWindow()) return;
    pushSessionState();
    sendToMainRenderer('backend:status', backendStatusPayload(backendConnected));
  });

  win.on('hide', () => {
    if (mainWindow === win) browserHost?.hidePanel();
  });
  const prepareWindowsSandboxWhenVisible = () => {
    queueMicrotask(() => {
      if (!currentWindow()) return;
      const setup = windowsSandboxSetup.startAutomatically({
        platform: process.platform,
        packaged: app.isPackaged,
        visible: windowShowRequested && win.isVisible() && !win.isMinimized(),
        ci: Boolean(process.env['CI']),
      }, () => {
        const { ACE_SECURITY_RUNTIME: runtimePath, ACE_SECURITY_STATE_DIR: stateDir } =
          packagedSecurityRuntimeEnv({ bundledOnly: true });
        return runtimePath ? { runtimePath, stateDir } : undefined;
      });
      void setup?.then((result) => {
        if (!result.ok) console.warn('[security] Windows initial sandbox setup:', result.code ?? result.exitCode);
      }).catch(() => {
        console.warn('[security] Windows initial sandbox setup could not complete');
      });
    });
  };
  win.on('show', () => {
    if (!currentWindow()) return;
    sendToMainRenderer('browser-view:layout-invalidated');
    prepareWindowsSandboxWhenVisible();
  });
  win.on('restore', prepareWindowsSandboxWhenVisible);
  win.on('closed', () => {
    releaseRendererTransports(senderId);
    gatewaySocketGenerations.delete(senderId);
    browserSocketGenerations.delete(senderId);
    if (mainWindow !== win) return;
    rendererRecovery.close(documentId);
    exhaustedRenderer = null;
    browserHost?.hidePanel();
    mainWindow = null;
  });
  win.on('close', (event) => {
    if (mainWindow !== win || desktopShutdown.canCloseWindows) return;
    if (isQuitting) {
      event.preventDefault();
      return;
    }
    if (rendererAuthState().isLoggedIn && win.isVisible()) {
      if (shouldPreventClose()) {
        event.preventDefault();
        return;
      }
    } else if (process.platform === 'darwin') {
      return;
    }
    // Let Electron finish this close event before the shutdown gate replays app.quit().
    event.preventDefault();
    setImmediate(() => app.quit());
  });

  // Keep a primary window registered while retiring the old one so replacement
  // never triggers window-all-closed and shuts down the surviving Gateway.
  previous?.destroy();
  const initialDocumentId = documentId;
  void win.loadFile(expected).catch((error: NodeJS.ErrnoException) => {
    if (currentWindow() && error.code !== 'ERR_ABORTED') {
      rendererRecovery.fail(initialDocumentId, { reason: 'load-failed' });
    }
  });
  if (IS_DEV_LAUNCH && !previous && windowShowRequested) {
    contents.openDevTools({ mode: 'detach' });
  }
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

function packagedSecurityRuntimeEnv({ bundledOnly = false } = {}): Record<string, string> {
  const devStateDir = IS_DEV_LAUNCH && !app.isPackaged ? process.env['ACE_SECURITY_STATE_DIR']?.trim() : undefined;
  const stateDir = devStateDir && path.isAbsolute(devStateDir) ? devStateDir : path.join(app.getPath('userData'), 'security');
  const strictSecurity = isStrictSecurityEnabled() ? '1' : '0';
  const configured = bundledOnly ? '' : String(process.env['ACE_SECURITY_RUNTIME'] ?? '').trim();
  const runtimeName = process.platform === 'win32'
    ? 'ace-security-runtime.exe'
    : 'ace-security-runtime';
  const platformKey = securityRuntimePlatformKey(process.platform, process.arch);
  const runtimeCandidates = configured
    ? [configured]
    : app.isPackaged
      ? [path.join(process.resourcesPath, runtimeName)]
      : [
          path.join(repoRoot(), 'desktop', 'security-runtime-bin', runtimeName),
          ...(platformKey ? [path.join(repoRoot(), 'security-runtime', 'prebuilt', platformKey, runtimeName)] : []),
          path.join(repoRoot(), 'security-runtime', 'bin', runtimeName),
        ];
  const runtime = runtimeCandidates.find((candidate) => path.isAbsolute(candidate) && fs.existsSync(candidate)) ?? '';
  if (!runtime || !path.isAbsolute(runtime) || !fs.existsSync(runtime)) {
    return {
      ACE_SECURITY_STATE_DIR: stateDir,
      ACE_STRICT_SECURITY: strictSecurity,
    };
  }
  const manifestPath = path.join(path.dirname(runtime), 'runtime-manifest.json');
  if (bundledOnly && !fs.existsSync(manifestPath)) {
    return { ACE_SECURITY_STATE_DIR: stateDir, ACE_STRICT_SECURITY: strictSecurity };
  }
  if (fs.existsSync(manifestPath)) {
    try {
      const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8')) as {
        files?: Array<{ name?: string; sha256?: string }>;
        binary_name?: string;
        binary_sha256?: string;
        platform?: string;
        arch?: string;
      };
      const declaresPlatform = Boolean(manifest.platform || manifest.arch);
      if (
        declaresPlatform
        && (!platformKey || securityRuntimePlatformKey(manifest.platform ?? '', manifest.arch ?? '') !== platformKey)
      ) {
        return {
          ACE_SECURITY_STATE_DIR: stateDir,
          ACE_STRICT_SECURITY: strictSecurity,
        };
      }
      const expected = manifest.files?.find((item) => item.name === runtimeName)?.sha256
        ?? (manifest.binary_name === runtimeName ? manifest.binary_sha256 : undefined);
      const actual = createHash('sha256').update(fs.readFileSync(runtime)).digest('hex');
      if (!expected || actual !== expected) {
        return {
          ACE_SECURITY_STATE_DIR: stateDir,
          ACE_STRICT_SECURITY: strictSecurity,
        };
      }
    } catch {
      return {
        ACE_SECURITY_STATE_DIR: stateDir,
        ACE_STRICT_SECURITY: strictSecurity,
      };
    }
  }
  const result: Record<string, string> = {
    ACE_SECURITY_RUNTIME: runtime,
    ACE_SECURITY_STATE_DIR: stateDir,
    ACE_STRICT_SECURITY: strictSecurity,
  };
  if (fs.existsSync(manifestPath)) {
    try {
      const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8')) as {
        files?: Array<{ name?: string; sha256?: string }>;
      };
      const bwrap = manifest.files?.find((item) => item.name === 'bwrap');
      if (bwrap?.sha256) {
        result['ACE_BUNDLED_BWRAP'] = path.join(process.resourcesPath, 'bwrap');
        result['ACE_BUNDLED_BWRAP_SHA256'] = bwrap.sha256;
      }
    } catch {
      // Malformed release metadata leaves the bundled fallback unavailable.
    }
  }
  return result;
}

/** Browser control is privileged even on loopback; bind it to this Gateway instance. */
function gatewayAccessHeaders(pathname: string): Record<string, string> {
  const headers: Record<string, string> = {};
  const sessionCookie = loginNewServiceInstance.cookieHeader();
  if (sessionCookie) headers.Cookie = sessionCookie;
  if (!pathname.startsWith('/api/browser/')) return headers;
  headers.Authorization = `Bearer ${gatewayInstanceAccessToken(activeGatewayCrewHome())}`;
  return headers;
}

/** Sign privileged requests with the same instance key as the active Gateway. */
function createActiveGatewaySecurityProof(method: string, pathname: string, body: string): string {
  return createDesktopSecurityProof(method, pathname, body, {
    crewHome: activeGatewayCrewHome(),
  });
}

/**
 * 最近一次经 health 线程端口探测拿到的 loop_lag_ms：断连日志里带上它，
 * 便于区分「进程死」与「进程活但业务循环忙」（IPC payload 结构不变）。
 */
let lastGatewayLoopLagMs: number | undefined;

function gatewayProbeOptions(child: ChildProcessWithoutNullStreams | null): GatewayInstanceVerificationOptions {
  const crewHome = activeGatewayCrewHome();
  if (!child) return { crewHome, rejectDesktopManaged: true };
  const desktopLaunchKey = gatewayLaunchKeys.get(child);
  if (!desktopLaunchKey) throw new Error('Missing managed Gateway launch key');
  return { crewHome, desktopLaunchKey };
}

function spawnOwnedGateway(command: string, args: string[], options: SpawnOptionsWithoutStdio): ChildProcessWithoutNullStreams {
  const key = randomBytes(32);
  const child = spawn(command, [...args, '--desktop-parent-lease'], {
    ...options,
    stdio: 'pipe',
    env: { ...options.env, CREW_DESKTOP_GATEWAY_LEASE_KEY: key.toString('hex') },
  });
  gatewayLaunchKeys.set(child, key);
  child.stdin.on('error', (err) => console.warn('[gateway] parent lease pipe:', err.message));
  return child;
}

function handleManagedGatewayEnd(
  child: ChildProcessWithoutNullStreams,
  platform: string,
  exit: { code: number | null; signal: NodeJS.Signals | null } | { error: string },
): void {
  if (managedGateway !== child) return;
  resetGatewayAttempt();
  managedGateway = null;
  ensureGatewayPromise = null;
  requireOwnedGateway = true;
  clearStalledRecycle();
  backendHealthMonitor.markRestart();
  gatewayComponents = undefined;
  lastGatewayLoopLagMs = undefined;
  const failed = 'error' in exit;
  backendProcessState = failed ? 'error' : 'offline';
  pushBackendStatus(false, { healthState: 'unknown' });
  logSupervisorDecision(failed ? 'instance-error' : 'instance-exit', { platform, ...exit });
  if (!isQuitting) {
    gatewayRestartController.noteGatewayExit();
    gatewayRestartController.schedule();
  }
}

// 供开发态 / 回退使用的 Gateway
function startManagedGateway(port: number): void {
  if (managedGateway) return;
  setBackendProcessState('starting');
  const root = repoRoot();
  const python = candidatePython();
  const crewHome = activeGatewayCrewHome();
  const env = {
    ...process.env,
    CREW_HOME: crewHome,
    GATEWAY_PORT: String(port),
    PYTHONPATH: root,
    ...packagedSecurityRuntimeEnv(),
    ...managedGatewayModeEnv(
      gatewayIdentityMode,
      crewHome,
    ),
    // Windows 控制台默认 GBK：强制 UTF-8，避免 Rich 日志写 emoji 时 UnicodeEncodeError 刷屏
    PYTHONIOENCODING: 'utf-8',
    ...(process.platform === 'win32' ? { PYTHONUTF8: '1' } : {}),
    // 不注入 CREW_TASK_WORKSPACE_ROOT，让后端从 config.yaml 自行计算
  };
  managedGateway = spawnOwnedGateway(python, ['-m', 'crew.gateway.server'], {
    cwd: root,
    env,
    windowsHide: true,
  });
  const child = managedGateway;
  attachGatewayLog(child, '[gateway]');
  writeGatewayLogLine(`[spawn] managed gateway pid=${child.pid} port=${port} python=${python}`);
  child.on('exit', (code, signal) => {
    console.warn('[gateway] exited', { code, signal });
    handleManagedGatewayEnd(child, 'managed', { code, signal });
  });
  child.on('error', (error) => {
    console.error('[gateway] managed gateway process error:', error);
    if (child.pid === undefined) handleManagedGatewayEnd(child, 'managed', { error: String(error) });
  });
}

/**
 * 打包态 gateway 统一布局（三平台一致，源码模式，不再使用 PyInstaller 二进制）：
 *   <gatewayRoot>/crew/            — gateway 源码树（含 skills/scenarios/mcp_servers）
 *   <gatewayRoot>/config/          — 可发布配置模板（.env.example / config.yaml.example / prompts/）
 *   <gatewayRoot>/plugins/         — 内置插件
 *   <gatewayRoot>/runtimes/python/ — 内嵌 Python（gateway 本体与技能脚本共用同一解释器）
 *   <gatewayRoot>/runtimes/node/   — 内嵌 Node.js（技能脚本用）
 * gateway 由安装包内嵌 Python 以 `-m crew.gateway.server` 直接跑源码启动。
 * CREW_PACKAGED=1 让后端识别打包态（用户配置落 ~/ 而非安装目录）。
 */
function packagedGatewayLaunch(gatewayRoot: string): {
  command: string;
  args: string[];
  envExtras: Record<string, string>;
} {
  const python = process.platform === 'win32'
    ? path.join(gatewayRoot, 'runtimes', 'python', 'python.exe')
    : path.join(gatewayRoot, 'runtimes', 'python', 'bin', 'python3');
  return {
    command: python,
    args: ['-m', 'crew.gateway.server'],
    envExtras: {
      CREW_PACKAGED: '1',
      PYTHONPATH: gatewayRoot,
      // 安装目录可能只读（Program Files / /opt），禁止向源码树写 .pyc
      PYTHONDONTWRITEBYTECODE: '1',
    },
  };
}

// ============================================================================
// 🌟 核心新增：专供 Windows 打包态使用的绝对路径静默启动
// ============================================================================
/**
 * Windows 打包版专用：在指定端口启动 gateway（内嵌 Python 跑源码）。
 * 通过 GATEWAY_PORT 环境变量告知后端监听端口。
 */
function startWindowsPackagedGateway(port: number): void {
  if (managedGateway) return;
  setBackendProcessState('starting');

  const exeDir = path.dirname(app.getPath('exe'));
  const gatewayRoot = path.resolve(exeDir, '..', 'crew-gateway');
  const launch = packagedGatewayLaunch(gatewayRoot);

  console.log(`[gateway] Starting packaged Windows gateway on port ${port}:`, launch.command);

  try {
    const spawnStart = Date.now();
    managedGateway = spawnOwnedGateway(launch.command, launch.args, {
      cwd: gatewayRoot,
      windowsHide: true,
      detached: false,
      // 不注入 CREW_TASK_WORKSPACE_ROOT，让后端从 config.yaml 自行计算
      env: {
        ...process.env,
        CREW_HOME: resolveCrewHome(),
        GATEWAY_PORT: String(port),
        ...managedGatewayModeEnv(gatewayIdentityMode, activeGatewayCrewHome()),
        ...launch.envExtras,
        ...packagedSecurityRuntimeEnv(),
        // 与开发态一致：内嵌 Python 仍可能走 GBK 控制台
        PYTHONIOENCODING: 'utf-8',
        ...(process.platform === 'win32' ? { PYTHONUTF8: '1' } : {}),
      },
    });
    const child = managedGateway;
    console.log(`[gateway] Spawn took ${Date.now() - spawnStart}ms, PID: ${child.pid}`);

    // 记录 stdout/stderr 用于诊断启动问题
    attachGatewayLog(child, '[gateway-win]');
    writeGatewayLogLine(`[spawn] packaged win gateway pid=${child.pid} port=${port} python=${launch.command}`);

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
      handleManagedGatewayEnd(child, 'win32', { code, signal });
    });
    child.on('error', (err) => {
      console.error('[gateway] Failed to start Windows packaged gateway:', err);
      if (child.pid === undefined) handleManagedGatewayEnd(child, 'win32', { error: String(err) });
    });
  } catch (err) {
    console.error('[gateway] Exception while spawning Windows gateway:', err);
  }
}

/**
 * macOS 打包版专用：在指定端口启动 gateway（内嵌 Python 跑源码）。
 * macOS .app 结构：crew-desktop.app/Contents/Resources/crew-gateway/
 */
function startMacOSPackagedGateway(port: number): void {
  if (managedGateway) return;
  setBackendProcessState('starting');

  // macOS .app bundle: exe 位于 Contents/MacOS/crew-desktop，
  // gateway 源码树放在 Contents/Resources/crew-gateway/
  const resourcesPath = path.join(path.dirname(app.getPath('exe')), '..', 'Resources');
  const gatewayRoot = path.join(resourcesPath, 'crew-gateway');
  const launch = packagedGatewayLaunch(gatewayRoot);

  console.log(`[gateway] Starting packaged macOS gateway on port ${port}:`, launch.command);

  try {
    managedGateway = spawnOwnedGateway(launch.command, launch.args, {
      cwd: gatewayRoot,
      detached: false,
      env: {
        ...process.env,
        CREW_HOME: resolveCrewHome(),
        GATEWAY_PORT: String(port),
        ...managedGatewayModeEnv(gatewayIdentityMode, activeGatewayCrewHome()),
        ...launch.envExtras,
        ...packagedSecurityRuntimeEnv(),
      },
    });
    const child = managedGateway;

    attachGatewayLog(child, '[gateway-mac]');
    writeGatewayLogLine(`[spawn] packaged mac gateway pid=${child.pid} port=${port} python=${launch.command}`);

    child.on('exit', (code, signal) => {
      console.warn('[gateway] macOS packaged gateway exited', { code, signal });
      handleManagedGatewayEnd(child, 'darwin', { code, signal });
    });
    child.on('error', (err) => {
      console.error('[gateway] Failed to start macOS packaged gateway:', err);
      if (child.pid === undefined) handleManagedGatewayEnd(child, 'darwin', { error: String(err) });
    });
  } catch (err) {
    console.error('[gateway] Exception while spawning macOS gateway:', err);
  }
}

/**
 * Linux 打包版专用：在指定端口启动 gateway（内嵌 Python 跑源码）。
 * deb 装在固定路径 /opt/crew-gateway/（源码树 + runtimes/）。
 *
 * 与 macOS/Windows 一致：desktop 子进程托管 gateway。废弃了原先的 systemd user
 * service 架构——多用户机器上各用户 systemd 服务都 enable-linger + Restart=always
 * 会抢同一个 8000 端口，且跨用户 instance key 不一致导致桌面端验签失败。
 * 改为 desktop 启动时 spawn、退出时随之消亡，彻底消除多用户常驻冲突。
 */
function startLinuxPackagedGateway(port: number): void {
  if (managedGateway) return;
  setBackendProcessState('starting');

  const gatewayRoot = '/opt/crew-gateway';
  const launch = packagedGatewayLaunch(gatewayRoot);

  console.log(`[gateway] Starting packaged Linux gateway on port ${port}:`, launch.command);

  try {
    managedGateway = spawnOwnedGateway(launch.command, launch.args, {
      cwd: gatewayRoot,
      detached: false,
      env: {
        ...process.env,
        CREW_HOME: resolveCrewHome(),
        GATEWAY_PORT: String(port),
        ...managedGatewayModeEnv(gatewayIdentityMode, activeGatewayCrewHome()),
        ...launch.envExtras,
        ...packagedSecurityRuntimeEnv(),
      },
    });
    const child = managedGateway;

    attachGatewayLog(child, '[gateway-linux]');
    writeGatewayLogLine(`[spawn] packaged linux gateway pid=${child.pid} port=${port} python=${launch.command}`);

    child.on('exit', (code, signal) => {
      console.warn('[gateway] Linux packaged gateway exited', { code, signal });
      handleManagedGatewayEnd(child, 'linux', { code, signal });
    });
    child.on('error', (err) => {
      console.error('[gateway] Failed to start Linux packaged gateway:', err);
      if (child.pid === undefined) handleManagedGatewayEnd(child, 'linux', { error: String(err) });
    });
  } catch (err) {
    console.error('[gateway] Exception while spawning Linux gateway:', err);
  }
}

let gatewayLog: GatewayStartupLog | null = null;

function gatewayLogPath(): string {
  return path.join(app.getPath('userData'), 'logs', 'gateway-startup.log');
}

function writeGatewayLogLine(line: string): void {
  gatewayLog?.line(line);
}

function attachGatewayLog(child: ChildProcessWithoutNullStreams, consoleLabel: string): void {
  gatewayLog ??= new GatewayStartupLog(gatewayLogPath());
  gatewayLog.attach(child, consoleLabel);
}

/** 统一 backend:status 负载：connected + 实际 baseUrl + 启动日志路径（供 renderer「查看日志」）。
 *  各平台 gateway 均由 desktop 子进程托管，gateway stdout/stderr 落盘到
 *  userData/logs/gateway-startup.log，「查看日志」指向它即可。 */
function backendLogInfo(): { logPath: string } {
  return { logPath: gatewayLogPath() };
}

type BackendStatusDetail = {
  failureKind?: GatewayProbeFailureKind;
  since?: number;
  healthState?: BackendHealthState;
  loopLagMs?: number;
  transportState?: BackendTransportState;
};

function backendStatusPayload(
  connected: boolean,
  detail: BackendStatusDetail = {},
): {
  connected: boolean;
  baseUrl: string;
  logPath: string;
  components?: Record<string, GatewayComponentState>;
  failureKind?: GatewayProbeFailureKind;
  since?: number;
  processState: BackendProcessState;
  healthState: BackendHealthState;
  transportState: BackendTransportState;
  readiness: DesktopGatewayReadiness;
  loopLagMs?: number;
} {
  return {
    connected,
    baseUrl: resolvedGatewayBaseUrl,
    ...backendLogInfo(),
    ...(gatewayComponents ? { components: gatewayComponents } : {}),
    ...(detail.failureKind ? { failureKind: detail.failureKind } : {}),
    ...(detail.since !== undefined ? { since: detail.since } : {}),
    processState: backendProcessState,
    healthState: detail.healthState ?? backendHealthState,
    transportState: detail.transportState ?? backendTransportState,
    readiness: gatewayReadiness.snapshot,
    ...(detail.loopLagMs !== undefined
      ? { loopLagMs: detail.loopLagMs }
      : lastGatewayLoopLagMs !== undefined ? { loopLagMs: lastGatewayLoopLagMs } : {}),
  };
}

function setBackendProcessState(next: BackendProcessState): void {
  if (backendProcessState === next) return;
  backendProcessState = next;
  try {
    mainWindow?.webContents.send('backend:status', backendStatusPayload(backendConnected));
  } catch {
    // webContents may be destroyed during shutdown
  }
}

function setBackendTransportState(next: BackendTransportState): void {
  if (backendTransportState === next) return;
  backendTransportState = next;
  try {
    mainWindow?.webContents.send('backend:status', backendStatusPayload(backendConnected));
  } catch {
    // webContents may be destroyed during shutdown
  }
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
 * 每个等待都有死线（冷启动可拖到 90s+ 的 AV 扫描已按 180s 上限覆盖）：
 *   - spawn 后的冷启动等待：180s（GATEWAY_COLD_START_TIMEOUT_MS）
 *   - 复用存活实例的等待：60s（进程声称活着却迟迟不健康，按卡死处理）
 * 死线触发时若子进程仍活着则先收割再返回 false——否则下一轮 ensureGateway 会
 * 在「旧实例占端口」的状态下扫出新端口，端口被无谓顶替。
 * 也响应：ready / 子进程退出 / 应用退出 / 所属 ensureGateway 代际被作废（用户重试）。
 */
class GatewaySupersededError extends Error {
  constructor() { super('gateway attempt superseded'); }
}

function assertGatewayGeneration(generation: number): void {
  if (isQuitting || generation !== gatewayGeneration) throw new GatewaySupersededError();
}

function resetGatewayAttempt(): number {
  const generation = ++gatewayGeneration;
  resetGatewayAuthRefresh();
  gatewayReadiness.reset(generation);
  closeGatewaySockets('gateway-replaced');
  backendTransportState = 'unknown';
  return generation;
}

function recordCoreReadiness(
  generation: number,
  status: DesktopGatewayReadiness['status'],
  probe?: GatewayInstanceProbe,
  errorCode?: string,
): void {
  assertGatewayGeneration(generation);
  const changed = gatewayReadiness.observe(generation, status, errorCode);
  if (probe?.verified) {
    gatewayComponents = probe.components ?? gatewayComponents;
    if (!backendConnected || changed) {
      if (status === 'ready') backendProcessState = 'ready';
      else if (status === 'failed') backendProcessState = 'error';
      pushBackendStatus(true);
    }
  } else if (changed && status === 'failed') {
    backendProcessState = 'error';
    pushBackendStatus(backendConnected);
  }
}

const GATEWAY_COLD_START_TIMEOUT_MS = 180_000;
const GATEWAY_HEALTH_PORT_UNAVAILABLE = 'GATEWAY_HEALTH_PORT_UNAVAILABLE';
const GATEWAY_REUSE_WAIT_TIMEOUT_MS = 60_000;

async function waitForGatewayReady(
  baseUrl: string,
  child: ChildProcessWithoutNullStreams | null,
  generation: number,
  timeoutMs: number,
  initialProbe?: GatewayInstanceProbe,
): Promise<GatewayBinding> {
  const started = Date.now();
  let attempts = 0;
  const deadline = started + timeoutMs;
  const childIsCurrent = () =>
    child === null || (managedGateway === child && child.pid !== undefined
      && child.exitCode === null && child.signalCode === null);
  const options = gatewayProbeOptions(child);
  while (Date.now() < deadline) {
    assertGatewayGeneration(generation);
    if (!childIsCurrent()) throw new GatewaySupersededError();
    attempts++;
    const probe = initialProbe ?? await probeGatewayInstance(baseUrl, options);
    initialProbe = undefined;
    assertGatewayGeneration(generation);
    if (!childIsCurrent()) throw new GatewaySupersededError();
    const readiness = readinessFromMainProbe(probe, child !== null);
    if (readiness !== undefined) {
      const status = readiness?.status ?? 'ready';
      const errorCode = status === 'failed' ? readiness?.errorCode ?? 'GATEWAY_STARTUP_FAILED' : undefined;
      if (child && errorCode === GATEWAY_HEALTH_PORT_UNAVAILABLE) {
        throw new GatewayStartupError(errorCode);
      }
      recordCoreReadiness(generation, status, probe, errorCode);
      if (status === 'ready') {
        console.log(`[gateway] Core ready after ${Date.now() - started}ms (${attempts} attempts)`);
        return { baseUrl, managed: child !== null, serverGeneration: readiness?.generation ?? null };
      }
      if (errorCode) throw new GatewayStartupError(errorCode);
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  assertGatewayGeneration(generation);
  logSupervisorDecision('health-wait-timeout', { waitedMs: Date.now() - started, attempts });
  if (childIsCurrent() && child) await stopManagedGateway('health-wait-timeout');
  throw new GatewayStartupError('GATEWAY_STARTUP_TIMEOUT');
}

function stopManagedGateway(reason: string, graceMs?: number): Promise<void> {
  if (gatewayRetirement.pending) return gatewayRetirement.pending;
  const child = managedGateway ?? gatewayRetirement.child;
  if (!child) return Promise.resolve();
  if (managedGateway === child) {
    managedGateway = null;
    gatewayRestartController.noteIntentionalStop();
  }
  const pid = child.pid ?? -1;
  const generation = gatewayGeneration;
  return gatewayRetirement.stop(child, graceMs).then(() => {
    logSupervisorDecision('instance-exit-superseded', { pid, reason, generation });
  }, (error: unknown) => {
    logSupervisorDecision('instance-stop-failed', { pid, reason, generation });
    if (!isQuitting && generation === gatewayGeneration) setBackendProcessState('error');
    throw error;
  });
}

function beginGatewayReplacement(reason: string, owned: boolean): number {
  const generation = resetGatewayAttempt();
  const loopLagMs = lastGatewayLoopLagMs;
  requireOwnedGateway ||= owned;
  ensureGatewayPromise = null;
  clearStalledRecycle();
  backendHealthMonitor.markRestart();
  gatewayComponents = undefined;
  lastGatewayLoopLagMs = undefined;
  backendProcessState = 'restarting';
  pushBackendStatus(false, { healthState: 'unknown' });
  logSupervisorDecision(reason, {
    generation,
    owned,
    ...(reason === 'stalled-recycle' && loopLagMs !== undefined ? { loopLagMs } : {}),
  });
  return generation;
}

function recycleOwnedGateway(child: ChildProcessWithoutNullStreams, reason: string): void {
  if (managedGateway !== child || isQuitting) return;
  const generation = beginGatewayReplacement(reason, true);
  void gatewayRestartController.scheduleAfter(
    stopManagedGateway(reason),
    () => !isQuitting && generation === gatewayGeneration,
  ).catch((error: unknown) => {
    console.error('[gateway] managed recycle failed:', error);
  });
}

// ============================================================================
// Backend health monitor: periodically polls /api/health and pushes status
// changes to the renderer via IPC ('backend:status').
// 探针串行化（链式 setTimeout）、失败分类（unreachable/timeout/auth_failed/unknown）
// 与启动/稳定双阈值详见 backend-health-monitor.ts。
// ============================================================================
const backendHealthMonitor = new BackendHealthMonitor(
  // 每次探测取当时的 resolvedGatewayBaseUrl（ensureGateway 端口选择后会更新）
  () => (gatewayRetirement.child || (!managedGateway && !canReuseExternalGateway()))
    ? Promise.resolve({ verified: false, failureKind: 'unreachable' } as const)
    : probeBackendHealth(resolvedGatewayBaseUrl, gatewayProbeOptions(managedGateway)),
  (change) => {
    gatewayComponents = change.connected ? change.components : undefined;
    if (change.healthState) backendHealthState = change.healthState;
    if (change.connected) lastGatewayLoopLagMs = change.loopLagMs;
    pushBackendStatus(change.connected, {
      ...(change.failureKind ? { failureKind: change.failureKind } : {}),
      ...(change.since !== undefined ? { since: change.since } : {}),
      ...(change.healthState ? { healthState: change.healthState } : {}),
      ...(change.loopLagMs !== undefined ? { loopLagMs: change.loopLagMs } : {}),
    });
    updateStalledRecycle(change);
  },
);

// ── 卡死自动回收：进程活着但业务循环 stalled（loop_lag ≥10s 双样本去抖）────────
// 持续 30s 仍 stalled 时只回收本次探测对应的子进程。
const STALLED_RECYCLE_AFTER_MS = 30_000;
let stalledRecycleTimer: ReturnType<typeof setTimeout> | null = null;

function clearStalledRecycle(): void {
  if (stalledRecycleTimer !== null) clearTimeout(stalledRecycleTimer);
  stalledRecycleTimer = null;
}

function updateStalledRecycle(change: { connected: boolean; healthState?: BackendHealthState }): void {
  const stalled = change.connected && change.healthState === 'stalled';
  if (!stalled) {
    clearStalledRecycle();
    return;
  }
  const child = managedGateway;
  if (stalledRecycleTimer !== null || isQuitting || !child) return;
  const generation = gatewayGeneration;
  stalledRecycleTimer = setTimeout(() => {
    stalledRecycleTimer = null;
    if (generation !== gatewayGeneration || backendHealthState !== 'stalled') return;
    recycleOwnedGateway(child, 'stalled-recycle');
  }, STALLED_RECYCLE_AFTER_MS);
}

function pushBackendStatus(
  connected: boolean,
  detail: BackendStatusDetail = {},
): void {
  backendConnected = connected;
  if (detail.healthState) backendHealthState = detail.healthState;
  if (detail.loopLagMs !== undefined) lastGatewayLoopLagMs = detail.loopLagMs;
  console.log(
    `[main] backend status → ${connected ? 'connected' : `disconnected (${detail.failureKind ?? 'unknown'})`}`
    + (!connected && lastGatewayLoopLagMs !== undefined
      ? `，最近 loop_lag_ms=${Math.round(lastGatewayLoopLagMs)}`
      : ''),
  );
  try {
    mainWindow?.webContents.send('backend:status', backendStatusPayload(connected, detail));
  } catch {
    // webContents may be destroyed
  }
}

function startBackendHealthMonitor(): void {
  backendHealthMonitor.start();
}

function stopBackendHealthMonitor(): void {
  clearStalledRecycle();
  backendHealthMonitor.stop();
}

// ============================================================================
// 🌟 核心改造：双平台极致适配的 Gateway 路由
// ============================================================================
function canReuseExternalGateway(): boolean {
  return !requireOwnedGateway && shouldProbeExternalGateway(gatewayIdentityMode)
    && (!app.isPackaged || process.platform === 'darwin');
}

async function launchGateway(generation: number): Promise<GatewayBinding> {
  assertGatewayGeneration(generation);
  const existing = managedGateway;
  if (existing) {
    const baseUrl = resolvedGatewayBaseUrl;
    logSupervisorDecision('reuse-alive-instance', { pid: existing.pid ?? -1, generation });
    return waitForGatewayReady(baseUrl, existing, generation, GATEWAY_REUSE_WAIT_TIMEOUT_MS);
  }

  const platform = app.isPackaged ? process.platform : null;
  const starter = platform === 'win32' ? startWindowsPackagedGateway
    : platform === 'linux' ? startLinuxPackagedGateway
      : platform === 'darwin' ? startMacOSPackagedGateway : null;
  if (canReuseExternalGateway()) {
    const probe = await probeGatewayInstance(DEFAULT_GATEWAY_URL, gatewayProbeOptions(null));
    assertGatewayGeneration(generation);
    if (probe.verified) {
      resolvedGatewayBaseUrl = DEFAULT_GATEWAY_URL;
      return waitForGatewayReady(DEFAULT_GATEWAY_URL, null, generation, GATEWAY_REUSE_WAIT_TIMEOUT_MS, probe);
    }
  }

  const firstPort = starter ? GATEWAY_PORT_MIN : MANAGED_GATEWAY_PORT;
  const end = Math.min(firstPort + GATEWAY_PORT_RANGE_SIZE, 65535);
  const deadline = Date.now() + GATEWAY_COLD_START_TIMEOUT_MS;
  let portFailure: GatewayStartupError | undefined;
  for (let port = firstPort; port < end; port++) {
    const [gatewayAvailable, healthAvailable] = await Promise.all([
      isPortAvailable(port), isPortAvailable(port + 1),
    ]);
    assertGatewayGeneration(generation);
    if (!gatewayAvailable || !healthAvailable) continue;
    if (Date.now() >= deadline) throw new GatewayStartupError('GATEWAY_STARTUP_TIMEOUT');

    const baseUrl = `http://127.0.0.1:${port}`;
    backendHealthMonitor.markRestart();
    resolvedGatewayBaseUrl = baseUrl;
    if (IS_DEV_LAUNCH) process.env.GATEWAY_PORT = String(port);
    if (starter) starter(port);
    else startManagedGateway(port);
    const child = managedGateway;
    if (!child) throw new GatewayStartupError('GATEWAY_LAUNCH_FAILED');

    try {
      return await waitForGatewayReady(baseUrl, child, generation, deadline - Date.now());
    } catch (error) {
      if (!(error instanceof GatewayStartupError) || error.code !== GATEWAY_HEALTH_PORT_UNAVAILABLE) throw error;
      assertGatewayGeneration(generation);
      if (managedGateway !== child) throw new GatewaySupersededError();
      logSupervisorDecision('health-port-unavailable', { port: port + 1, generation });
      await stopManagedGateway('health-port-unavailable');
      assertGatewayGeneration(generation);
      portFailure = error;
    }
  }
  if (portFailure) throw portFailure;
  throw new Error(`No available Gateway port in ${firstPort}–${end - 1}`);
}

async function ensureGateway(): Promise<GatewayBinding> {
  const generation = gatewayGeneration;
  assertGatewayGeneration(generation);
  if (gatewayReadiness.snapshot.status === 'failed') {
    throw new GatewayStartupError(gatewayReadiness.snapshot.errorCode ?? 'GATEWAY_STARTUP_FAILED');
  }
  if (gatewayRetirement.pending) {
    await gatewayRetirement.pending;
    return ensureGateway();
  }
  if (gatewayRetirement.child) throw new GatewayStopTimeoutError(gatewayRetirement.child.pid);
  if (!managedGateway && gatewayRestartController.isTripped()) {
    throw new Error('Gateway automatic recovery stopped; manual retry is required');
  }

  const cachedPromise = ensureGatewayPromise;
  if (cachedPromise) {
    const cached = await cachedPromise;
    // Re-prove cached endpoints before using them for credential-bearing requests.
    // A successful proof for an older generation cannot authorize traffic.
    for (let attempt = 0; attempt < HEALTH_FAIL_THRESHOLD; attempt++) {
      assertGatewayGeneration(generation);
      if (ensureGatewayPromise !== cachedPromise) return ensureGateway();
      const probe = await probeGatewayInstance(
        cached.baseUrl, gatewayProbeOptions(cached.managed ? managedGateway : null),
      );
      assertGatewayGeneration(generation);
      if (ensureGatewayPromise !== cachedPromise) return ensureGateway();
      if (probe.verified && (
        cached.serverGeneration === null ? !probe.readiness
          : probe.readiness?.status === 'ready' && probe.readiness.generation === cached.serverGeneration
      )) return cached;
      if (probe.verified && probe.readiness) break;
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
    assertGatewayGeneration(generation);
    if (ensureGatewayPromise !== cachedPromise) return ensureGateway();
    const replacement = beginGatewayReplacement('instance-reprobe', cached.managed);
    if (cached.managed) await stopManagedGateway('health-reproof-failed');
    assertGatewayGeneration(replacement);
    return ensureGateway();
  }

  const tracked: Promise<GatewayBinding> = launchGateway(generation).then((result) => {
    assertGatewayGeneration(generation);
    if (ensureGatewayPromise !== tracked) throw new Error('Gateway changed before readiness');
    if (result.managed) {
      requireOwnedGateway = false;
      gatewayRestartController.noteGatewayReady();
    }
    void refreshGatewayAuth(result.baseUrl).catch((error: unknown) => {
      if (!isQuitting && generation === gatewayGeneration) console.warn('[auth] Gateway config refresh failed:', error);
    });
    return result;
  }).catch((error: unknown) => {
    if (ensureGatewayPromise !== tracked) throw error;
    ensureGatewayPromise = null;
    if (isQuitting || generation !== gatewayGeneration || error instanceof GatewaySupersededError) throw error;
    const startupError = error instanceof GatewayStartupError
      ? error : new GatewayStartupError('GATEWAY_LAUNCH_FAILED');
    if (startupError !== error) startupError.cause = error;
    recordCoreReadiness(generation, 'failed', undefined, startupError.code);
    throw startupError;
  });
  ensureGatewayPromise = tracked;
  return tracked;
}

/**
 * 冷启动/重启窗口内的有界重试 fetch。
 * 「健康线程就绪」先于主 API 端口绑定（uvicorn 在 lifespan 返回后才 listen），
 * 此窗口内打到 gateway 的请求会吃 ECONNREFUSED——这是进程启动与端口绑定的
 * 时间差，不是故障。仅在网络级拒连且进程处于 starting/restarting 时等待重试；
 * HTTP 状态错误与其他异常原样抛出，offline/error 态快速失败。
 */
const GATEWAY_FETCH_STARTUP_RETRY_MS = 15_000;
const GATEWAY_FETCH_STARTUP_RETRY_INTERVAL_MS = 250;
const GATEWAY_FETCH_RETRYABLE_CODES = new Set(['ECONNREFUSED', 'ECONNRESET', 'EPIPE']);

function isGatewayStartupRetryable(error: unknown): boolean {
  let current: unknown = error;
  while (current && typeof current === 'object') {
    const code = (current as { code?: unknown }).code;
    if (typeof code === 'string' && GATEWAY_FETCH_RETRYABLE_CODES.has(code)) return true;
    current = (current as { cause?: unknown }).cause;
  }
  return false;
}

function gatewayFetchRetryWindowOpen(): boolean {
  return !isQuitting
    && (backendProcessState === 'starting' || backendProcessState === 'restarting');
}

async function fetchGatewayWithStartupRetry(url: string, init?: RequestInit): Promise<Response> {
  const deadline = Date.now() + GATEWAY_FETCH_STARTUP_RETRY_MS;
  for (;;) {
    try {
      return await fetch(url, init);
    } catch (error) {
      if (!isGatewayStartupRetryable(error) || !gatewayFetchRetryWindowOpen() || Date.now() >= deadline) {
        throw error;
      }
      await new Promise((resolve) => setTimeout(resolve, GATEWAY_FETCH_STARTUP_RETRY_INTERVAL_MS));
    }
  }
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
    ...(requestPath.startsWith('/api/security/')
      ? { 'X-Crew-Security-Proof': createActiveGatewaySecurityProof(method, requestPath, body) }
      : {}),
    ...(body ? { 'content-type': 'application/json' } : {}),
    ...(usesRemoteAuth ? { Authorization: `Bearer ${jwt}`, ...identityHeaders } : {}),
  };
  // A lost decision response can only be reconciled by reading its receipt.
  const isDecision = method === 'POST' && pathname.endsWith('/decision');
  const request = isDecision ? fetch : fetchGatewayWithStartupRetry;
  const response = await request(`${baseUrl}${pathname}`, {
    method,
    headers,
    redirect: 'error',
    ...(isDecision || pathname.endsWith('/decision-receipt') ? { signal: AbortSignal.timeout(10_000) } : {}),
    ...(body ? { body } : {}),
  });
  const responseBody = await response.json().catch(() => ({ detail: response.statusText }));
  return { ok: response.ok, status: response.status, body: responseBody };
}

function parseOrThrow<T>(parsed: { ok: true; value: T } | { ok: false; error: string }, channel: string): T {
  if (parsed.ok) return parsed.value;
  const err = new Error(`${IPC_ARG_VALIDATION_FAILED}: ${channel} ${parsed.error}`);
  console.warn(`[ipc] ${channel} rejected: ${parsed.error}`);
  throw err;
}

type TrustedIpcEvent = Electron.IpcMainEvent | Electron.IpcMainInvokeEvent;
type TrustedIpcHandler = (event: Electron.IpcMainInvokeEvent, ...args: unknown[]) => unknown;

function rendererDocumentLease(event: TrustedIpcEvent): () => boolean {
  const win = mainWindow;
  const frame = event.senderFrame;
  const sender = event.sender;
  const documentId = rendererDocumentId;
  return () => !isQuitting && win !== null && win === mainWindow && !win.isDestroyed()
    && rendererRecovery.canServe(documentId) && sender === win.webContents
    && !sender.isCrashed() && frame === sender.mainFrame;
}

function assertTrustedRenderer(event: TrustedIpcEvent): void {
  const senderFrame = event.senderFrame;
  if (!mainWindow || !senderFrame || event.sender.id !== mainWindow.webContents.id || senderFrame !== mainWindow.webContents.mainFrame) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: untrusted IPC sender`);
  }
  if (!rendererRecovery.canServe(rendererDocumentId)) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: renderer document is no longer active`);
  }
  const expectedFile = path.join(__dirname, '../assets/index.html');
  if (!isTrustedRendererFileUrl(senderFrame.url, expectedFile)) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: IPC sender must be the packaged renderer`);
  }
}

const rawIpcHandle = ipcMain.handle.bind(ipcMain);

function trustedHandle(channel: string, listener: TrustedIpcHandler): void {
  rawIpcHandle(channel, (event, ...args) => {
    assertTrustedRenderer(event);
    if (isQuitting && channel !== 'app:quit') throw new Error('Desktop is shutting down');
    return listener(event, ...args);
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
  return path.join(app.getPath('userData'), 'updates');
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
          fs.rmSync(path.join(dir, name), { force: true });
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
        fs.rmSync(full, { force: true });
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
 * 安装前做完整性 + magic-byte 校验；失败保留包供重试，成功则清空 downloaded。
 */
async function installDownloadedUpdate(expectedVersion?: string): Promise<VersionUpdatePackageResult> {
  const downloaded = installableUpdateState(currentAppVersion(app)).downloaded;
  if (downloaded && expectedVersion !== undefined && downloaded.version !== expectedVersion) {
    return { success: false, message: '待安装的更新版本已变更，请重新确认' };
  }
  if (!downloaded) {
    return { success: false, message: '没有已下载的更新包' };
  }

  const preparation = desktopShutdown.beginHandoff('update');
  if (!preparation) return { success: false, message: '应用正在退出或安装更新' };
  let launched = false;
  sendVersionUpdateDownloadProgress({ phase: 'installing', percent: 100 });
  try {
    await preparation;
    const current = installableUpdateState(currentAppVersion(app)).downloaded;
    if (!current || current.filePath !== downloaded.filePath || current.version !== downloaded.version
      || current.size !== downloaded.size) {
      throw new Error('待安装的更新包已经变更，请重试');
    }
    if (isStrictSecurityEnabled()) {
      const publicKey = configuredUpdatePublicKey();
      if (!publicKey) throw new Error('无法验证安装包签名：未配置更新签名公钥');
      const signature = verifyPackageSignature(current.filePath, `${current.filePath}.sig`, publicKey);
      if (!signature.ok) throw new Error(signature.message);
    }
    const handoff = await launchUpdateInstaller(path.resolve(downloaded.filePath), {
      openPath: (filePath) => shell.openPath(filePath),
    });
    launched = true;
    if (handoff.kind === 'installer-started') {
      try {
        if (readUpdateState().downloaded?.filePath === downloaded.filePath) setDownloadedRecord(null);
      } catch (error) {
        console.warn('[update-install] Could not clear the launched package receipt:', error);
      }
    }
    sendVersionUpdateDownloadProgress({ phase: 'completed', percent: 100 });
    const message = handoff.kind === 'package-opened' ? '安装包已打开'
      : handoff.restartScheduled === false ? '安装程序已启动，安装后请手动打开应用'
        : '安装程序已启动';
    return { success: true, message };
  } catch (error) {
    const message = (error as Error).message || '启动安装失败';
    sendVersionUpdateDownloadProgress({ phase: 'error', message });
    return { success: false, message };
  } finally {
    desktopShutdown.finishHandoff(launched);
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
  ipcMain.on('inspiration:sticky-close', (event) => {
    const entry = Array.from(inspirationWindows.entries()).find(
      ([, win]) => !win.isDestroyed() && win.webContents.id === event.sender.id,
    );
    if (entry) closeInspirationWindow(entry[0]);
  });
  trustedHandle('app:quit', () => app.quit());
  trustedHandle('app:get-version', () => ({
    version: currentAppVersion(app),
    label: currentAppVersionLabel(app),
  }));
  trustedHandle('app:get-launch-mode', () => ({
    isDevLaunch: IS_DEV_LAUNCH,
    mode: IS_DEV_LAUNCH ? 'dev' : 'account',
  }));
  ipcMain.on('window:maximized', (e) => {
    assertTrustedRenderer(e);
    e.sender.send('window:maximized-changed', true);
  });
  ipcMain.on('window:unmaximized', (e) => {
    assertTrustedRenderer(e);
    e.sender.send('window:maximized-changed', false);
  });

  trustedHandle('shell:openExternal', externalLinkBridge.register((url) => {
    return shell.openExternal(url);
  }));

  trustedHandle('shell:openPath', (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'shell:openPath');
    const extraRoots = args.allowedRoot ? [args.allowedRoot] : [];
    const resolved = resolveShellAllowedPath(args.path, extraRoots);
    return shell.openPath(resolved);
  });

  trustedHandle('dialog:saveLocalExport', async (event, raw: unknown) => {
    const documentIsCurrent = rendererDocumentLease(event);
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
    if (!documentIsCurrent() || currentCrewFileOwnerSegment() !== ownerSegment) return { ok: false, canceled: true };
    const result = await dialog.showSaveDialog(mainWindow!, {
      title: '分享灵感',
      defaultPath: path.join(app.getPath('downloads'), args.suggestedName),
      filters: [{ name: 'ZIP 压缩包', extensions: ['zip'] }],
    });
    if (result.canceled || !result.filePath || !documentIsCurrent() || currentCrewFileOwnerSegment() !== ownerSegment) {
      return { ok: false, canceled: true };
    }
    await fs.promises.copyFile(source, result.filePath);
    return { ok: true, canceled: false, path: result.filePath };
  });

  trustedHandle('tracing:save-export', async (event, raw: unknown) => {
    const documentIsCurrent = rendererDocumentLease(event);
    const owner = currentBrowserOwnerId();
    const args = parseOrThrow(TracingSaveExportArgs.parse(raw), 'tracing:save-export');
    const { baseUrl } = await ensureGateway();
    const generation = gatewayGeneration;
    const isCurrent = () => documentIsCurrent() && owner === currentBrowserOwnerId() && generation === gatewayGeneration;
    if (!isCurrent()) return { ok: false, canceled: true };
    const exportPath = `/api/tracing/exports/${args.exportId}`;
    const buildGatewayHeaders = (): Record<string, string> => {
      const headers: Record<string, string> = { ...gatewayAccessHeaders(exportPath) };
      const identity = usesGatewayRemoteAuth() ? gatewayIdentityHeaders() : null;
      if (usesGatewayRemoteAuth() && !identity) throw new Error('追踪导出需要有效登录态');
      Object.assign(headers, identity || {});
      return headers;
    };
    const statusResponse = await fetchGatewayWithStartupRetry(`${baseUrl}${exportPath}`, {
      headers: buildGatewayHeaders(),
    });
    if (!statusResponse.ok) {
      const detail = (await statusResponse.text().catch(() => '')).slice(0, 500);
      throw new Error(`追踪导出状态查询失败 (${statusResponse.status})${detail ? `: ${detail}` : ''}`);
    }
    const statusBody = await statusResponse.json() as { status?: string; format?: string };
    if (statusBody.status !== 'completed' && statusBody.status !== 'partial') {
      throw new Error('追踪导出尚未完成');
    }
    const format = statusBody.format === 'json' || statusBody.format === 'csv' || statusBody.format === 'jsonl'
      ? statusBody.format
      : 'jsonl';
    const extension = format === 'jsonl' ? 'jsonl' : format;
    if (!isCurrent()) return { ok: false, canceled: true };
    const result = await dialog.showSaveDialog(mainWindow!, {
      title: '保存追踪导出',
      defaultPath: path.join(app.getPath('downloads'), `ace-traces-${args.exportId}.${extension}`),
      filters: [
        { name: 'JSONL', extensions: ['jsonl', 'ndjson'] },
        { name: 'JSON', extensions: ['json'] },
        { name: 'CSV', extensions: ['csv'] },
      ],
    });
    if (result.canceled || !result.filePath || !isCurrent()) return { ok: false, canceled: true };

    const downloadPath = `${exportPath}/download`;
    const response = await fetchGatewayWithStartupRetry(`${baseUrl}${downloadPath}`, {
      headers: buildGatewayHeaders(),
    });
    if (!isCurrent()) {
      await response.body?.cancel().catch(() => undefined);
      return { ok: false, canceled: true };
    }
    if (!response.ok) {
      const detail = (await response.text().catch(() => '')).slice(0, 500);
      throw new Error(`追踪导出下载失败 (${response.status})${detail ? `: ${detail}` : ''}`);
    }
    const declaredLength = Number(response.headers.get('content-length') || 0);
    const maxBytes = 64 * 1024 * 1024;
    if (declaredLength > maxBytes) throw new Error('追踪导出超过大小上限');
    if (!response.body) throw new Error('追踪导出没有可读取的数据流');

    let totalBytes = 0;
    const limiter = new Transform({
      transform(chunk: Buffer | string, _encoding, callback) {
        const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        totalBytes += buffer.byteLength;
        if (totalBytes > maxBytes) {
          callback(new Error('追踪导出超过大小上限'));
          return;
        }
        callback(null, buffer);
      },
    });
    const tempPath = `${result.filePath}.${args.exportId}.${process.hrtime.bigint().toString()}.tmp`;
    let committed = false;
    try {
      await pipeline(
        Readable.fromWeb(response.body as unknown as NodeReadableStream),
        limiter,
        fs.createWriteStream(tempPath, { flags: 'wx', mode: 0o600 }),
      );
      if (!isCurrent()) return { ok: false, canceled: true };
      await commitDownloadedExport(tempPath, result.filePath);
      committed = true;
    } finally {
      if (!committed) await fs.promises.unlink(tempPath).catch(() => undefined);
    }
    return { ok: true, canceled: false, path: result.filePath };
  });

  trustedHandle('shell:readTextFile', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'shell:readTextFile');
    const resolved = resolveShellAllowedPath(args.path);
    const stat = fs.lstatSync(resolved);
    const maxBytes = 512 * 1024;
    if (!stat.isFile() || stat.nlink !== 1 || fs.realpathSync(args.path) !== resolved) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:readTextFile target is not a file`);
    }
    if (stat.size > maxBytes) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:readTextFile file too large`);
    }
    const bytes = await readResolvedCrewFile({
      filePath: resolved,
      identity: {
        dev: stat.dev,
        ino: stat.ino,
        nlink: stat.nlink,
        size: stat.size,
        mtimeMs: stat.mtimeMs,
        ctimeMs: stat.ctimeMs,
      },
    });
    return bytes?.toString('utf8') ?? '';
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
      resolved = resolveShellAllowedPath(args.path);
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

  trustedHandle('shell:showItemInFolder', (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'shell:showItemInFolder');
    const resolved = resolveShellAllowedPath(args.path);
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
    const res = await fetchGatewayWithStartupRetry(new URL(listPath, ensured.baseUrl).toString(), {
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

  trustedHandle('dialog:selectFile', async (_e, raw: unknown) => {
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
      return r.filePaths;
    }

    const items = await Promise.all(
      r.filePaths.map(async (filePath) => {
        try {
          const stat = await fs.promises.stat(filePath);
          if (stat.size > maxBytes) {
            return {
              path: filePath,
              name: path.basename(filePath),
              dataUrl: '',
              error: `FILE_TOO_LARGE: ${stat.size} > ${maxBytes}`,
            };
          }
          const buffer = await fs.promises.readFile(filePath);
          const ext = path.extname(filePath).slice(1).toLowerCase();
          const mimeType = mimeFromExt(ext);
          if (!ALLOWED_MIME_TYPES.has(mimeType)) {
            return {
              path: filePath,
              name: path.basename(filePath),
              dataUrl: '',
              error: `MIME_NOT_ALLOWED: ${mimeType}`,
            };
          }
          return {
            path: filePath,
            name: path.basename(filePath),
            dataUrl: `data:${mimeType};base64,${buffer.toString('base64')}`,
          };
        } catch (err) {
          return {
            path: filePath,
            name: path.basename(filePath),
            dataUrl: '',
            error: `READ_FAILED: ${(err as Error).message}`,
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
  trustedHandle('tray:set-status', (_e, status: unknown) => {
    if (!isTrayStatus(status)) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: tray:set-status expected a known tray status`);
    }
    setTrayStatus(status);
    return { ok: true };
  });
  trustedHandle('tray:set-notifications', (_e, summary: unknown) => {
    if (!isTrayNotificationSummary(summary)) {
      throw new Error(
        `${IPC_ARG_VALIDATION_FAILED}: tray:set-notifications expected a valid notification summary`,
      );
    }
    trayService?.setNotifications(summary);
    return { ok: true };
  });
  trustedHandle('app:get-system-locale', () => {
    return app.getLocale();
  });
  trustedHandle('app:renderer-initial-state-ready', () => {
    if (!rendererRecovery.isReady(rendererDocumentId)) {
      rendererRecovery.ready(rendererDocumentId);
      revealMainWindowIfReady();
    }
    return { ok: true };
  });
  ipcMain.on('renderer:heartbeat', (event) => {
    try { assertTrustedRenderer(event); } catch { return; }
    rendererRecovery.heartbeat(rendererDocumentId);
  });
  trustedHandle('update:start-download', async (_e, raw: unknown) => {
    const args = parseOrThrow(UpdateStartDownloadArgs.parse(raw), 'update:start-download');
    return startUpdateDownload({ version: args.version, type: args.type, url: args.url });
  });

  trustedHandle('update:pause', () => pauseUpdateDownload());
  trustedHandle('update:resume', () => resumeUpdateDownload());
  trustedHandle('update:retry', async (_e, raw: unknown) => {
    const args = parseOrThrow(UpdateStartDownloadArgs.parse(raw), 'update:retry');
    return retryUpdateDownload({ version: args.version, type: args.type, url: args.url });
  });

  trustedHandle('update:install-package', async (_event, version) => {
    if (version !== undefined && typeof version !== 'string') throw new Error('无效的更新版本');
    return installDownloadedUpdate(version);
  });

  trustedHandle('update:get-state', (): UpdateStateSnapshot => installableUpdateState(currentAppVersion(app)));

  // 打开 GitHub Releases 当前系统对应的安装包下载页。查询失败 / 无匹配资产时回退到 releases 列表页。
  trustedHandle('release:open-latest-download', async () => {
    const fallback = { ok: false, url: githubReleasesPageUrl() };
    try {
      const res = await fetch(`https://api.github.com/repos/${GITHUB_REPO}/releases/latest`, {
        headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'Crew-Desktop' },
        signal: AbortSignal.timeout(15_000),
      });
      if (!res.ok) return fallback;
      const release = (await res.json()) as { assets?: Array<{ name: string; browser_download_url: string }> };
      const assets = release.assets ?? [];
      const target = resolveGithubReleaseTarget();
      const url = resolveGithubReleaseDownloadUrl(assets, target) ?? githubReleasesPageUrl();
      return { ok: true, url };
    } catch {
      return fallback;
    }
  });

  trustedHandle('feedback:submit', async (_e, raw: unknown) => {
    const args = parseOrThrow(FeedbackSubmitArgs.parse(raw), 'feedback:submit');
    return submitFeedback(args);
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
      const { baseUrl } = await ensureGateway();
      await refreshGatewayAuth(baseUrl);
    } catch {
      // 心跳不阻塞 UI；失败时沿用旧会话态
    }
    pushSessionState();
    scheduleBrowserHostConnection();
    return { success: true };
  });

  // First paint depends on the last known auth state, not a cold Gateway.
  // The successful startup attempt refreshes and publishes verified auth separately.
  trustedHandle('auth:get-state', () => {
    if (gatewayReadiness.snapshot.status === 'ready') {
      void refreshGatewayAuth(resolvedGatewayBaseUrl).catch(() => {});
    }
    return { ok: true, state: rendererAuthState() };
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
    const { baseUrl } = await ensureGateway();
    const authState = loginNewServiceInstance.getState();
    if (authState.mode === 'remote' && !code) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: auth:login code required`);
    }
    const result = authState.mode === 'email'
      ? await loginNewServiceInstance.loginWithEmail(baseUrl, identifier)
      : await loginNewServiceInstance.login(baseUrl, identifier, code);
    await pushSessionState(result.ok ? 'login-changed' : undefined);
    if (result.ok) scheduleBrowserHostConnection();
    return result;
  });

  trustedHandle('auth:logout', async () => {
    const { baseUrl } = await ensureGateway();
    const result = await loginNewServiceInstance.logout(baseUrl);
    await pushSessionState(result.ok ? 'logout' : undefined);
    return result;
  });

  trustedHandle('gateway:fetch', async (event, raw: unknown) => {
    const documentIsCurrent = rendererDocumentLease(event);
    const owner = currentBrowserOwnerId();
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
        'origin', 'referer', 'content-length', 'x-crew-security-proof',
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
    fetchInit.headers = {
      ...(fetchInit.headers as Record<string, string> | undefined),
      ...gatewayAccessHeaders(targetUrl.pathname),
    };
    if (args.init?.body !== undefined) fetchInit.body = args.init.body;
    const proofMethod = (args.init?.method || 'GET').toUpperCase();
    const proofPath = targetUrl.pathname;
    const proofBody = typeof args.init?.body === 'string' ? args.init.body : '';
    const cuaAuthority = classifyCuaSetupAuthorityRequest(proofMethod, proofPath, proofBody);
    if (cuaAuthority !== null) {
      const generation = gatewayGeneration;
      const isCurrent = () => documentIsCurrent() && owner === currentBrowserOwnerId() && generation === gatewayGeneration;
      const confirmed = isCurrent() && (cuaAuthority !== 'install' || await confirmCuaDriverInstall(mainWindow));
      if (!confirmed || !isCurrent()) {
        return {
          ok: false,
          status: 403,
          statusText: 'Forbidden',
          body: JSON.stringify({ ok: false, error: '用户取消了 CUA Driver 安装' }),
          headers: { 'content-type': 'application/json' },
        };
      }
      fetchInit.headers = {
        ...(fetchInit.headers as Record<string, string> | undefined),
        'X-Crew-Security-Proof': createActiveGatewaySecurityProof(proofMethod, proofPath, proofBody),
      };
    }
    const res = await fetchGatewayWithStartupRetry(targetUrl.toString(), fetchInit);
    const body = await res.text();
    return {
      ok: res.ok,
      status: res.status,
      statusText: res.statusText,
      body,
      headers: Object.fromEntries(res.headers.entries()),
    };
  });

  trustedHandle('gateway:stream-start', async (event, raw: unknown) => {
    const rawRequest = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {};
    const requestId = typeof rawRequest.request_id === 'string' ? rawRequest.request_id : '';
    if (!/^[A-Za-z0-9_.:-]{1,200}$/.test(requestId)) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid gateway stream request id`);
    }
    const args = parseOrThrow(GatewayFetchArgs.parse(raw), 'gateway:stream-start');
    const senderId = event.sender.id;
    const documentIsCurrent = rendererDocumentLease(event);
    if (!documentIsCurrent()) return { ok: false };
    const streams = gatewayStreamControllers.get(senderId) ?? new Map<string, AbortController>();
    gatewayStreamControllers.set(senderId, streams);
    const controller = new AbortController();
    const previous = streams.get(requestId);
    streams.set(requestId, controller);
    previous?.abort();
    const isActive = () => documentIsCurrent() && !controller.signal.aborted
      && streams.get(requestId) === controller;
    const send = (payload: Record<string, unknown>) => {
      if (isActive()) {
        event.sender.send('gateway:stream-event', { request_id: requestId, ...payload });
      }
    };
    try {
      const usesRemoteAuth = usesGatewayRemoteAuth();
      const jwt = usesRemoteAuth ? loginNewServiceInstance.getJWTToken() : null;
      const identityHeaders = usesRemoteAuth ? gatewayIdentityHeaders() : null;
      if (usesRemoteAuth && (!jwt || !identityHeaders)) {
        send({ type: 'error', error: '未登录或登录用户缺少身份信息' });
        return { ok: false };
      }
      const ensured = await ensureGateway();
      if (!isActive()) return { ok: false };
      const targetUrl = new URL(args.url);
      const ensuredUrl = new URL(ensured.baseUrl);
      targetUrl.protocol = ensuredUrl.protocol;
      targetUrl.hostname = ensuredUrl.hostname;
      targetUrl.port = ensuredUrl.port;
      const headers: Record<string, string> = {};
      const denylist = new Set([
        'host', 'connection', 'cookie', 'authorization',
        'origin', 'referer', 'content-length',
      ]);
      for (const [key, value] of Object.entries(args.init?.headers || {})) {
        if (!denylist.has(key.toLowerCase())) headers[key] = value;
      }
      if (usesRemoteAuth && jwt && identityHeaders) {
        headers.Authorization = `Bearer ${jwt}`;
        Object.assign(headers, identityHeaders);
      }
      Object.assign(headers, gatewayAccessHeaders(targetUrl.pathname));
      const response = await fetchGatewayWithStartupRetry(targetUrl.toString(), {
        method: args.init?.method || 'GET',
        headers,
        ...(args.init?.body !== undefined ? { body: args.init.body } : {}),
        signal: controller.signal,
      });
      send({
        type: 'head',
        status: response.status,
        headers: Object.fromEntries(response.headers.entries()),
      });
      if (!response.body) {
        send({ type: 'end' });
        return { ok: response.ok };
      }
      const reader = response.body.getReader();
      try {
        const decoder = new TextDecoder();
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const text = decoder.decode(value, { stream: true });
          if (text) send({ type: 'chunk', text });
        }
        const tail = decoder.decode();
        if (tail) send({ type: 'chunk', text: tail });
      } finally {
        reader.releaseLock();
      }
      send({ type: 'end' });
      return { ok: response.ok };
    } catch (error) {
      if ((error as Error).name !== 'AbortError') {
        send({ type: 'error', error: (error as Error).message || 'Gateway 流请求失败' });
      }
      return { ok: false };
    } finally {
      if (streams.get(requestId) === controller) streams.delete(requestId);
      if (streams.size === 0 && gatewayStreamControllers.get(senderId) === streams) {
        gatewayStreamControllers.delete(senderId);
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
    const senderId = event.sender.id;
    const streams = gatewayStreamControllers.get(senderId);
    const controller = streams?.get(requestId);
    if (streams && controller) {
      streams.delete(requestId);
      if (streams.size === 0) gatewayStreamControllers.delete(senderId);
      controller.abort();
    }
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
  trustedHandle('gateway:upload', async (_e, raw: unknown) => {
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

    const maxMb = Math.round(GATEWAY_UPLOAD_MAX_FILE_BYTES / 1024 / 1024);
    const results: UploadFileResult[] = [];
    for (const filePath of args.files) {
      let content: Buffer;
      try {
        const stat = await fs.promises.stat(filePath);
        if (!stat.isFile()) {
          results.push(localFailure(filePath, 400, '不是普通文件'));
          continue;
        }
        if (stat.size === 0) {
          results.push(localFailure(filePath, 400, '文件为空'));
          continue;
        }
        if (stat.size > GATEWAY_UPLOAD_MAX_FILE_BYTES) {
          results.push(localFailure(filePath, 413, `文件超过大小上限（${maxMb} MB）`));
          continue;
        }
        content = await fs.promises.readFile(filePath);
      } catch (err) {
        results.push(localFailure(filePath, 400, `读取文件失败：${(err as Error).message}`));
        continue;
      }
      // 后端 /api/wiki/upload 逐文件接收（字段名 file），一次请求一个文件。
      const form = new FormData();
      form.append('file', new Blob([new Uint8Array(content)]), path.basename(filePath));
      try {
        const res = await fetchGatewayWithStartupRetry(uploadUrl, { method: 'POST', headers: authHeaders, body: form });
        results.push({
          path: filePath,
          ok: res.ok,
          status: res.status,
          statusText: res.statusText,
          body: await res.text(),
          headers: Object.fromEntries(res.headers.entries()),
        });
      } catch (err) {
        results.push(localFailure(filePath, 502, `上传请求失败：${(err as Error).message}`));
      }
    }
    return { results };
  });

  trustedHandle('gateway:ensure', async () => {
    const { baseUrl, managed } = await ensureGateway();
    return { baseUrl, managed };
  });
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
      if (requestId && nonce) securityApprovalNonces.set(requestId, nonce);
      const { nonce: _nonce, ...safe } = request;
      void _nonce;
      return safe;
    });
    return { ...result, body: { requests } };
  });

  let updatingDefaultPermission = false;
  let permissionAvailabilityRevision = 0;
  trustedHandle('security:get-full-access-choice', () => readSecurityPermissionPreferences());
  trustedHandle('security:set-full-access-choice', (_e, enabled: unknown) => {
    if (typeof enabled !== 'boolean') {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: security:set-full-access-choice requires a boolean`);
    }
    const preferences = saveFullAccessChoicePreference(enabled);
    permissionAvailabilityRevision += 1;
    return preferences;
  });
  trustedHandle('security:set-default-conversation-permission', async (event, mode: unknown) => {
    if (mode !== 'request_approval' && mode !== 'auto_review' && mode !== 'full_access') {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid default conversation permission`);
    }
    if (updatingDefaultPermission) return readSecurityPermissionPreferences();
    if (mode !== 'full_access') return saveDefaultConversationPermissionPreference(mode);
    const before = readSecurityPermissionPreferences();
    if (!before.enabled || before.defaultMode === mode) return before;
    const documentIsCurrent = rendererDocumentLease(event);
    const revision = permissionAvailabilityRevision;
    updatingDefaultPermission = true;
    try {
      if (!await confirmDefaultFullAccess(mainWindow) || !documentIsCurrent()
        || revision !== permissionAvailabilityRevision || !isFullAccessChoiceEnabled()) {
        return readSecurityPermissionPreferences();
      }
      return saveDefaultConversationPermissionPreference(mode);
    } finally {
      updatingDefaultPermission = false;
    }
  });

  trustedHandle('security:set-mode', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityModeArgs.parse(raw), 'security:set-mode');
    if (args.mode === 'full_access' && !isFullAccessChoiceEnabled()) {
      return { ok: false, status: 403, body: { detail: '设置中已关闭完全访问权限选项' } };
    }
    return securityGatewayRequest('PUT', '/api/security/mode', {
      workspace_id: args.workspaceId,
      session_id: args.sessionId,
      mode: args.mode,
    });
  });

  trustedHandle('security:decide', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityDecisionArgs.parse(raw), 'security:decide');
    const nonce = securityApprovalNonces.get(args.requestId);
    if (!nonce) return { ok: false, status: 409, body: { detail: '批准请求已过期或未加载' } };
    const pathname = `/api/security/requests/${encodeURIComponent(args.requestId)}/decision`;
    const result = await securityGatewayRequest('POST', pathname, {
      workspace_id: args.workspaceId,
      session_id: args.sessionId,
      task_id: args.taskId ?? '',
      nonce,
      decision: args.decision,
      ...(args.submissionId ? { submission_id: args.submissionId } : {}),
      ...(args.alwaysArgvPrefix ? { always_argv_prefix: args.alwaysArgvPrefix } : {}),
    });
    // 仅成功才删 nonce：409 可能是瞬时（task_id 时空不一致等），若删掉会让后续所有点击
    // 都命中上面的"已过期或未加载"分支而无法重试。409 时保留 nonce，交给下一次 /pending
    // 轮询与渲染层错误处理去对账（请求真死了轮询不再返回，overlay 自然撤掉）。
    if (result.ok) securityApprovalNonces.delete(args.requestId);
    return result;
  });

  trustedHandle('security:decision-receipt', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityDecisionReceiptArgs.parse(raw), 'security:decision-receipt');
    const pathname = `/api/security/requests/${encodeURIComponent(args.requestId)}/decision-receipt`;
    return securityGatewayRequest('POST', pathname, {
      workspace_id: args.workspaceId, session_id: args.sessionId, task_id: args.taskId ?? '',
      submission_id: args.submissionId, decision: args.decision, prefix_hash: args.prefixHash ?? '',
    });
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
    query.set('sort', args.sort ?? 'newest');
    return securityGatewayRequest('GET', `/api/security/audit?${query.toString()}`);
  });
  trustedHandle('security:audit-export', async () => securityGatewayRequest('GET', '/api/security/audit/export'));
  trustedHandle('security:audit-purge', async (_e, raw: unknown) => {
    const args = parseOrThrow(SecurityWorkspaceArgs.parse(raw), 'security:audit-purge');
    return securityGatewayRequest('POST', `/api/security/audit/purge-expired?workspace_id=${encodeURIComponent(args.workspaceId)}`);
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
    const { ACE_SECURITY_RUNTIME: runtime, ACE_SECURITY_STATE_DIR: stateDir } =
      packagedSecurityRuntimeEnv({ bundledOnly: app.isPackaged });
    if (!runtime || path.basename(runtime) !== 'ace-security-runtime.exe') {
      return {
        ok: false,
        exitCode: null,
        detail: app.isPackaged
          ? '随包 runtime 缺失，请重装或修复'
          : '未找到可用的 Windows 安全运行组件，请先构建并检查运行组件清单',
      };
    }
    return windowsSandboxSetup.run(args.action, runtime, stateDir);
  });
  // 手动重试接管自动重启，并和已经发起的回收等待同一个退出结果。
  trustedHandle('gateway:retry', async () => {
    if (isQuitting) return { ok: false };
    gatewayRestartController.reset();
    const generation = beginGatewayReplacement('user-retry', Boolean(managedGateway ?? gatewayRetirement.child));
    try {
      await stopManagedGateway('user-retry');
    } catch {
      if (!isQuitting && generation === gatewayGeneration) setBackendProcessState('error');
      return { ok: false };
    }
    if (isQuitting || generation !== gatewayGeneration) return { ok: true };
    void ensureGateway()
      .then(() => {
        assertGatewayGeneration(generation);
        scheduleBrowserHostConnection();
      })
      .catch((err: unknown) => {
        if (isQuitting || generation !== gatewayGeneration) return;
        console.error('[gateway] retry failed:', err);
        setBackendProcessState('error');
      });
    return { ok: true };
  });

  trustedHandle('chat-outbox:list', async () => {
    const scope = requireChatOutboxScope();
    const entries = await chatOutbox().list(scope);
    requireChatOutboxScope(scope);
    return { scope, entries };
  });

  trustedHandle('chat-outbox:put', async (_event, scope, entry) => {
    if (typeof scope !== 'string') throw new Error('Invalid chat outbox scope');
    const current = requireChatOutboxScope(scope);
    await chatOutbox().put(current, entry);
    await consumeSentCheckpoint(current, entry as DesktopChatOutboxEntry).catch(() => {
      // The send is already durable. Keep the journal as the source for draft
      // deduplication and retry the checkpoint update before retiring it.
      console.warn('[renderer-recovery] checkpoint-consume-deferred');
    });
  });

  trustedHandle('chat-outbox:remove', (_event, scope, requestId) => {
    if (typeof scope !== 'string') throw new Error('Invalid chat outbox scope');
    const current = requireChatOutboxScope(scope);
    return chatOutbox().remove(current, requestId, (entry) => consumeSentCheckpoint(current, entry));
  });

  trustedHandle('renderer-checkpoint:get', async (event) => {
    const currentDocument = rendererDocumentLease(event);
    const scope = requireChatOutboxScope();
    const checkpoint = await rendererCheckpoint().get(scope);
    requireChatOutboxScope(scope);
    if (!currentDocument()) throw new Error('Renderer document changed');
    return { scope, checkpoint };
  });

  trustedHandle('renderer-checkpoint:put', (_event, scope, checkpoint) => {
    if (typeof scope !== 'string') throw new Error('Invalid renderer checkpoint scope');
    return rendererCheckpoint().put(requireChatOutboxScope(scope), checkpoint);
  });

  trustedHandle('gateway-ws:connect', async (event) => {
    const senderId = event.sender.id;
    const documentIsCurrent = rendererDocumentLease(event);
    if (!documentIsCurrent()) return { ok: false, error: 'WebSocket 连接已取消' };
    const previous = gatewaySockets.get(senderId);
    const previousChat = previous ? gatewaySocketChat.get(previous) : undefined;
    const scope = currentChatOutboxScope();
    if (previous?.readyState === WebSocket.OPEN && previousChat?.scope === scope) {
      logMainStream('ws-connect-skip', { senderId, reason: 'already-open' });
      event.sender.send('gateway-ws:event', { type: 'open', gatewayGeneration });
      if (previousChat.capability) {
        event.sender.send('gateway-ws:event', {
          type: 'message', data: previousChat.capability.frame, gatewayGeneration,
        });
      }
      if (event.sender === mainWindow?.webContents) setBackendTransportState('connected');
      return { ok: true };
    }
    if (previous?.readyState === WebSocket.CONNECTING && previousChat?.scope === scope) return { ok: true };
    const gatewayAttempt = gatewayGeneration;
    const primaryTransport = event.sender === mainWindow?.webContents;
    const generation = ++rendererSocketAttempt;
    gatewaySocketGenerations.set(senderId, generation);
    const currentAttempt = () => !isQuitting && gatewayAttempt === gatewayGeneration
      && documentIsCurrent() && gatewaySocketGenerations.get(senderId) === generation;
    const setTransport = (state: BackendTransportState) => {
      if (primaryTransport && currentAttempt()) setBackendTransportState(state);
    };
    setTransport('reconnecting');
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
      setTransport('disconnected');
      return { ok: false, status: 401, error: '未登录' };
    }
    if (usesRemoteAuth && !gatewayIdentityHeaders()) {
      setTransport('disconnected');
      return { ok: false, status: 401, error: '登录信息缺失，请重新登录' };
    }
    let ensured: GatewayBinding;
    try {
      ensured = await ensureGateway();
    } catch (error) {
      setTransport('disconnected');
      throw error;
    }
    if (!currentAttempt() || currentChatOutboxScope() !== scope) {
      setTransport('disconnected');
      return { ok: false, error: 'WebSocket 连接已取消' };
    }
    const jwt = usesRemoteAuth ? loginNewServiceInstance.getJWTToken() : null;
    const identityHeaders = usesRemoteAuth ? gatewayIdentityHeaders() : null;
    if (usesRemoteAuth && (!jwt || !identityHeaders)) {
      setTransport('disconnected');
      return { ok: false, status: 401, error: '登录状态已变更' };
    }
    const httpUrl = new URL(ensured.baseUrl);
    httpUrl.protocol = httpUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    httpUrl.pathname = '/ws';
    httpUrl.search = '';
    httpUrl.searchParams.set('send_receipts', '1');
    httpUrl.hash = '';

    const socket = new WebSocket(httpUrl.toString(), {
      handshakeTimeout: 12_000,
      ...(usesRemoteAuth ? { headers: { Authorization: `Bearer ${jwt}`, ...identityHeaders } } : {}),
    });
    const socketChat: GatewaySocketChat = { scope };
    gatewaySocketChat.set(socket, socketChat);
    const socketOwner = currentBrowserOwnerId();
    gatewaySockets.set(senderId, socket);
    const currentSocket = () => currentAttempt() && gatewaySockets.get(senderId) === socket
      && currentBrowserOwnerId() === socketOwner;
    const sendEvent = (payload: Record<string, unknown>): void => {
      if (!currentSocket()) return;
      event.sender.send('gateway-ws:event', { ...payload, gatewayGeneration: gatewayAttempt });
    };
    const handleRendererDestroyed = (): void => {
      if (gatewaySocketGenerations.get(senderId) === generation) {
        setTransport('disconnected');
        gatewaySocketGenerations.delete(senderId);
      }
      if (gatewaySockets.get(senderId) === socket) gatewaySockets.delete(senderId);
      try {
        socket.close(1000, 'renderer-destroyed');
      } catch {
        // best-effort cleanup
      }
    };
    socket.on('open', () => {
      if (!currentSocket()) {
        socket.close(1000, 'stale-gateway-attempt');
        return;
      }
      logMainStream('ws-open', { senderId, url: httpUrl.toString() });
      sendEvent({ type: 'open' });
      setTransport('connected');
    });
    socket.on('message', (data) => {
      if (!currentSocket()) return;
      const frame = data.toString();
      let kind = 'unknown';
      let request_id: string | undefined;
      let session_id: string | undefined;
      let receiptStatus: string | undefined;
      let receiptCode: string | undefined;
      try {
        const parsed = JSON.parse(frame) as {
          kind?: string; request_id?: string; session_id?: string;
          body?: { version?: number; store_id?: unknown; request_stop?: unknown; status?: unknown; code?: unknown };
        };
        kind = parsed.kind ?? 'unknown';
        request_id = parsed.request_id;
        session_id = parsed.session_id;
        if (kind === 'send_receipt_capability' && parsed.body?.version === 1) {
          const storeId = parsed.body.store_id;
          if (typeof storeId === 'string' && /^[a-f0-9]{32}$/.test(storeId)) socketChat.capability = { frame, storeId, requestStop: parsed.body.request_stop === 1 };
        }
        if (kind === 'send_receipt') {
          const { status, code } = parsed.body ?? {};
          if (typeof status === 'string' && /^[a-z]{1,16}$/.test(status)) receiptStatus = status;
          if (typeof code === 'string' && /^[a-z0-9_]{1,64}$/.test(code)) receiptCode = code;
        }
      } catch {
        /* ignore parse for log */
      }
      if (kind !== 'ping' && kind !== 'pong') {
        logMainStream('ws-message', { kind, request_id, session_id, senderId, receiptStatus, receiptCode });
      }
      sendEvent({ type: 'message', data: frame });
    });
    socket.on('close', (code, reason) => {
      event.sender.removeListener('destroyed', handleRendererDestroyed);
      logMainStream('ws-close', { code, reason: reason.toString(), senderId });
      if (currentSocket()) setTransport('disconnected');
      sendEvent({
        type: 'close',
        code,
        reason: reason.toString(),
      });
      if (gatewaySocketGenerations.get(senderId) === generation) gatewaySocketGenerations.delete(senderId);
      if (gatewaySockets.get(senderId) === socket) gatewaySockets.delete(senderId);
    });
    socket.on('error', (err) => {
      if (currentSocket()) setTransport('disconnected');
      sendEvent({ type: 'error', error: err.message });
    });
    event.sender.once('destroyed', handleRendererDestroyed);
    return { ok: true };
  });

  trustedHandle('gateway-ws:send', (event, payload: unknown, outboxScope, outboxStoreId) => {
    const socket = gatewaySockets.get(event.sender.id);
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return { ok: false, error: 'WebSocket 未连接' };
    }
    const socketChat = gatewaySocketChat.get(socket);
    const currentScope = currentChatOutboxScope();
    if (!socketChat || socketChat.scope !== currentScope) {
      return { ok: false, error: 'WebSocket 账号已变更' };
    }
    if (payload && typeof payload === 'object' && 'action' in payload && payload.action === 'stop'
      && 'request_id' in payload && !socketChat.capability?.requestStop) {
      return { ok: false, error: '当前网关无法按回合停止' };
    }
    if (outboxScope !== undefined || outboxStoreId !== undefined) {
      if (typeof outboxScope !== 'string' || currentScope !== outboxScope || typeof outboxStoreId !== 'string'
        || socketChat.capability?.storeId !== outboxStoreId) {
        return { ok: false, error: 'WebSocket 消息受理不可用或账号已变更' };
      }
    }
    let data = '';
    try { data = typeof payload === 'string' ? payload : JSON.stringify(payload ?? {}); } catch {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: gateway WebSocket payload must be serializable`);
    }
    if (Buffer.byteLength(data, 'utf8') > MAX_CHAT_FRAME_BYTES) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: gateway WebSocket payload too large`);
    }
    try {
      socket.send(data);
      return { ok: true };
    } catch {
      return { ok: false, error: 'WebSocket 发送失败' };
    }
  });

  trustedHandle('gateway-ws:close', (event) => {
    const senderId = event.sender.id;
    gatewaySocketGenerations.delete(senderId);
    const socket = gatewaySockets.get(senderId);
    if (socket) {
      gatewaySockets.delete(senderId);
      try { socket.close(1000, 'client-close'); } catch { /* best effort */ }
    }
    if (event.sender === mainWindow?.webContents) setBackendTransportState('disconnected');
    return { ok: true };
  });

  // Browser state/debug uses a separate authenticated socket. Page rendering
  // and human input stay inside the sandboxed WebContentsView owned by BrowserHost.
  trustedHandle('browser-ws:connect', async (event, rawSessionId: unknown) => {
    const sessionId = validateBrowserSessionId(rawSessionId);
    const senderId = event.sender.id;
    const documentIsCurrent = rendererDocumentLease(event);
    if (!documentIsCurrent()) return { ok: false, error: '浏览器状态连接已取消' };
    const generation = ++rendererSocketAttempt;
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
    if (browserSocketGenerations.get(senderId) !== generation || !documentIsCurrent()) {
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
      headers: gatewayAccessHeaders('/api/browser/'),
      maxPayload: 2 * 1024 * 1024,
    });
    browserSockets.set(senderId, socket);
    const sendEvent = (payload: Record<string, unknown>): void => {
      if (
        browserSocketGenerations.get(senderId) !== generation
        || browserSockets.get(senderId) !== socket
        || !documentIsCurrent()
      ) return;
      event.sender.send('browser-ws:event', payload);
    };
    const handleRendererDestroyed = (): void => {
      if (browserSocketGenerations.get(senderId) === generation) {
        browserSocketGenerations.delete(senderId);
      }
      if (browserSockets.get(senderId) === socket) browserSockets.delete(senderId);
      try { socket.close(1000, 'renderer-destroyed'); } catch { /* best effort */ }
    };
    socket.on('open', () => sendEvent({ type: 'open', sessionId }));
    socket.on('message', (data) => sendEvent({ type: 'message', sessionId, data: data.toString() }));
    socket.on('close', (code, reason) => {
      event.sender.removeListener('destroyed', handleRendererDestroyed);
      if (browserSockets.get(senderId) !== socket) return;
      sendEvent({ type: 'close', sessionId, code, reason: reason.toString() });
      if (browserSocketGenerations.get(senderId) === generation) browserSocketGenerations.delete(senderId);
      browserSockets.delete(senderId);
    });
    socket.on('error', (error) => sendEvent({ type: 'error', sessionId, error: error.message }));
    event.sender.once('destroyed', handleRendererDestroyed);
    return { ok: true };
  });
  trustedHandle('browser-ws:close', (event) => {
    const senderId = event.sender.id;
    browserSocketGenerations.delete(senderId);
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
        error: (error instanceof Error ? error.message : '无法挂载浏览器页面').slice(0, 500),
      };
    }
  });
  trustedHandle('browser-view:hide', () => {
    browserHost?.hidePanel();
    return { ok: true };
  });
  trustedHandle('browser-view:zoom', (_event, raw: unknown) => {
    const request = validateBrowserPanelIdentity(raw);
    const action = (raw as Record<string, unknown>).action;
    if (action !== 'in' && action !== 'out' && action !== 'reset') {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid browser zoom action`);
    }
    const runtimeKey = currentBrowserRuntimeKey();
    if (!runtimeKey || !browserHost) return { ok: false, error: '桌面浏览器尚未连接' };
    try {
      return { ok: true, zoomFactor: browserHost.zoomPanel({ runtimeKey, ...request, action }) };
    } catch (error) {
      return { ok: false, error: (error instanceof Error ? error.message : '无法调整网页缩放').slice(0, 500) };
    }
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
        error: (error instanceof Error ? error.message : '无法读取浏览器导航状态').slice(0, 500),
      };
    }
  });

  loginNewServiceInstance.setVersionUpdateHandler((payload) => {
    // force 策略：持久化阻断锁，保证重启后仍强制更新直到本机版本达标
    if (payload.type === 'force' && payload.version) {
      setForceLock({
        requiredVersion: payload.version,
        ...(payload.url ? { url: payload.url } : {}),
        ...(payload.message ? { message: payload.message } : {}),
      });
    }
    mainWindow?.webContents.send('version-update-available', payload);
  });
  loginNewServiceInstance.setSessionExpiredHandler(async () => {
    await pushSessionState('session-expired');
  });
}

async function bootstrap() {
  await app.whenReady();
  if (isQuitting) return;
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
  // 非打包态同样在后台选择端口并启动受管 Gateway。

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
  configureUpdateController(sendVersionUpdateDownloadProgress, isStrictSecurityEnabled);
  sweepUpdatePartials();
  startUpdateCleanupMonitor();

  // 注入卸载模块依赖（托盘「卸载」功能需要访问主进程内部状态）
  setUninstallDeps({
    getMainWindow: () => mainWindow,
    beginHandoff: () => desktopShutdown.beginHandoff('uninstall'),
    finishHandoff: (launched) => desktopShutdown.finishHandoff(launched),
    suppressBackendOverlay: () => {
      try {
        mainWindow?.webContents.send('backend:suppress-overlay');
      } catch {
        // webContents may be destroyed
      }
    },
  });

  app.on('activate', () => {
    if (isQuitting) return;
    // Hidden AutomationHost windows are implementation details and must not
    // suppress recreation of the user-facing window on macOS.
    if (!mainWindow || mainWindow.isDestroyed()) {
      createWindow();
    }
    else showMainWindow();
  });
}

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin' && !isQuitting) app.quit();
});

async function prepareAppShutdown(reason: ShutdownReason): Promise<void> {
  isQuitting = true;
  resetGatewayAuthRefresh();
  gatewayReadiness.reset(++gatewayGeneration);
  ensureGatewayPromise = null;
  loginNewServiceInstance.stopHeartbeat();
  stopBackendHealthMonitor();
  stopUpdateCleanupMonitor();
  const sockets = closeGatewaySockets(reason);
  if (browserHostSocket) sockets.push(browserHostSocket);
  const child = managedGateway ?? gatewayRetirement.child;
  requireOwnedGateway ||= Boolean(child);
  console.log('[shutdown] preparing:', reason);

  const tasks = {
    sockets: settleClosingSockets(sockets),
    browser: waitForShutdownTask(resetBrowserHost(reason), 'Browser host', 3_000),
    download: waitForShutdownTask(disposeUpdateDownload(), 'Update download', 3_000),
    gateway: (async () => {
      await stopManagedGateway(reason, GATEWAY_APP_QUIT_GRACE_MS);
      if (child && child.exitCode !== 0) throw new Error('Gateway did not complete a graceful shutdown');
    })(),
  };
  const results = await Promise.allSettled(Object.values(tasks));
  if (gatewayLog) {
    await waitForShutdownTask(gatewayLog.flushAfterChildren(), 'Gateway log', 1500)
      .catch(error => console.warn('[shutdown] Gateway log drain incomplete:', error));
  }
  const failed: string[] = [];
  for (const [index, task] of Object.keys(tasks).entries()) {
    const result = results[index];
    if (result.status === 'rejected') {
      failed.push(task);
      console.error(`[shutdown] ${task} failed:`, result.reason);
    }
  }
  console.log('[shutdown] prepared', { reason, failed });
  if (failed.length) throw new Error(`未确认应用资源已关闭（${failed.join(', ')}）`);
}

async function settleClosingSockets(sockets: WebSocket[]): Promise<void> {
  const closing = sockets.filter((socket) => socket.readyState !== WebSocket.CLOSED);
  try {
    await waitForShutdownTask(Promise.all(closing.map((socket) =>
      new Promise<void>((resolve) => { socket.once('close', () => resolve()); }),
    )), 'Gateway sockets', 500);
  } catch {
    for (const socket of closing) {
      if (socket.readyState !== WebSocket.CLOSED) socket.terminate();
    }
  }
}

function resumeAfterHandoffFailure(): void {
  isQuitting = false;
  startBackendHealthMonitor();
  startUpdateCleanupMonitor();
  gatewayRestartController.reset();
  if (gatewayRetirement.child) {
    gatewayRetirement.child.once('exit', () => gatewayRestartController.schedule());
  } else {
    gatewayRestartController.schedule();
  }
  scheduleBrowserHostConnection();
}

function exitPreparedApp(): void {
  gatewayRestartController.stop();
  trayService?.dispose();
  for (const win of inspirationWindows.values()) {
    if (!win.isDestroyed()) win.destroy();
  }
  inspirationWindows.clear();
  if (BrowserWindow.getAllWindows().length === 0) {
    app.exit(0);
    return;
  }
  // Fall back if a native window ignores the replayed quit.
  const timeout = setTimeout(() => app.exit(0), 1_000);
  app.once('will-quit', () => clearTimeout(timeout));
  app.quit();
}

app.on('before-quit', (event) => desktopShutdown.beforeQuit(event));

if (process.argv.includes('--crew-provision-windows-sandbox')) {
  app.whenReady().then(async () => {
    if (!app.isPackaged || process.platform !== 'win32') {
      app.exit(2);
      return;
    }
    const { ACE_SECURITY_RUNTIME: runtime, ACE_SECURITY_STATE_DIR: stateDir } =
      packagedSecurityRuntimeEnv({ bundledOnly: true });
    if (!runtime) {
      app.exit(2);
      return;
    }
    const result = await provisionInstalledWindowsSandbox(runtime, stateDir);
    if (!result.ok) console.warn('[security] Windows sandbox setup:', result.detail ?? result.exitCode);
    app.exit(result.ok ? 0 : 1);
  }).catch((error: unknown) => {
    console.error('[security] Windows sandbox setup failed:', error);
    app.exit(1);
  });
} else if (process.argv.includes('--crew-packaging-smoke-test')) {
  // Load Electron and its main process without a window, Gateway or instance lock.
  // This catches macOS dyld/framework errors that codesign verification misses.
  app.whenReady().then(() => {
    if (!app.isPackaged) {
      console.error('Packaging smoke test requires a packaged app');
      app.exit(1);
      return;
    }
    process.stdout.write('CREW_PACKAGING_SMOKE_OK\n', () => app.exit(0));
  }).catch((err) => {
    console.error('Packaging smoke test failed:', err);
    app.exit(1);
  });
} else {
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
}
