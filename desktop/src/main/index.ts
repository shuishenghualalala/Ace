/**
 * Electron main process entry
 */
import { app, BrowserWindow, ipcMain, shell, dialog, Tray, Menu, nativeImage, nativeTheme, protocol } from 'electron';
import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import * as net from 'net';
import { createHash } from 'crypto';
import { spawn, type ChildProcessWithoutNullStreams } from 'child_process';
import WebSocket from 'ws';
import { BrowserHost, BrowserHostError } from './browser-host';
import { submitFeedback, getFeedbackList, getFeedbackImage } from './feedback-service';
import { registerCrewFileProtocol } from './crew-file-protocol';
import { registerSitePreviewProtocol } from './site-preview-protocol';
import { authorizeOwnedImagePath, writeOwnedImageToClipboard } from './image-clipboard';
import {
  managedGatewayModeEnv,
  resolveGatewayCrewHome,
  resolveGatewayIdentityMode,
  shouldProbeExternalGateway,
  type GatewayIdentityMode,
} from './gateway-launch-mode';
import { resolveCrewHome } from './crew-home';
import {
  gatewayInstanceAccessToken,
  probeGatewayInstance,
  type GatewayComponentState,
} from './gateway-instance-auth';
import { GatewayRestartController } from './gateway-restart-controller';
import { isTrustedRendererFileUrl } from './trusted-renderer-url';
import type {
  VersionUpdateDownloadProgressPayload,
  VersionUpdatePackageResult,
  UpdateStateSnapshot,
} from '../shared/types';
import {
  normalizeCloseBehavior,
  readDesktopPrefsFile,
  saveCloseBehaviorPreference,
  type CloseBehavior,
} from './desktop-prefs';
import { logMainStream } from './stream-debug';
import {
  GatewayFetchArgs,
  GatewayUploadArgs,
  ShellOpenExternalArgs,
  ShellOpenPathArgs,
  ShellOpenPathWithArgs,
  ShellWriteFileBase64Args,
  ShellWriteTextFileArgs,
  FeedbackSubmitArgs,
  FeedbackListArgs,
  FeedbackImageArgs,
  DialogSelectFileArgs,
  DialogSelectFolderArgs,
  DialogSaveLocalExportArgs,
  InspirationWindowArgs,
  UpdateStartDownloadArgs
} from '../shared/ipc-schemas';
import {
  GATEWAY_UPLOAD_MAX_FILE_BYTES,
  IPC_ARG_VALIDATION_FAILED,
  MAX_DIALOG_FILE_BYTES,
} from '../shared/constants';
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
import { verifyPackageIntegrity } from './update/update-integrity';
import { evaluateVersionUpdate } from './version-compare';
import { configurePptxWasmRuntime, PPTX_WASM_V8_FLAGS } from './wasm-runtime';
import { desktopAuthSession } from './auth-session';

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
let managedGateway: ChildProcessWithoutNullStreams | null = null;
let ensureGatewayPromise: Promise<{ baseUrl: string; managed: boolean }> | null = null;
const gatewaySockets = new Map<number, WebSocket>();
const gatewaySocketGenerations = new Map<number, number>();
const browserSockets = new Map<number, WebSocket>();
const browserSocketGenerations = new Map<number, number>();
let browserHost: BrowserHost | null = null;
let browserHostSocket: WebSocket | null = null;
let browserHostReconnectTimer: ReturnType<typeof setTimeout> | null = null;
let browserHostConnectionGeneration = 0;
let browserHostConnectPending = false;
let browserHostDisposePromise: Promise<void> | null = null;
let tray: Tray | null = null;
let rendererInitialStateReady = false;
let nativeWindowReady = false;
let windowShowRequested = true;
let rendererReadyFallbackTimer: ReturnType<typeof setTimeout> | null = null;

const RENDERER_READY_FALLBACK_MS = 15_000;

let isQuitting = false;
// Gateway 重建代际：每次主动重试/重建递增。在途 ensureGateway 流程启动时记下
// 自己的代际，health wait 每轮校验——代际变了说明用户点了重试，立即中止让位，
// 避免「旧 wait 无超时挂着、新重建又被排队」的假死。
let gatewayGeneration = 0;
// Backend health monitor state
let backendConnected = false;
let healthMonitorTimer: ReturnType<typeof setInterval> | null = null;
// 连续健康检查失败次数。单次 /api/health 超时不代表 gateway 挂了——gateway 繁忙
// （加载技能 / 构建大 prompt / 执行工具）时单线程 asyncio 可能 2s 内没响应 health。
// 需连续 N 次失败才判 disconnected，避免误弹「智能体运行环境准备中」遮罩。
let healthFailCount = 0;
const HEALTH_FAIL_THRESHOLD = 3;
let gatewayComponents: Record<string, GatewayComponentState> | undefined;
// Track the actually resolved gateway base URL (updated by ensureGateway)
let resolvedGatewayBaseUrl = DEFAULT_GATEWAY_URL;
// registerIpc() is called once at bootstrap, but defending against accidental
// re-invocation prevents duplicate ipcMain handlers (which would otherwise
// throw "attempt to register a second handler").
let ipcRegistered = false;

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
  for (const filename of ['config.yaml', 'config.yaml.example']) {
    try {
      const configPath = path.join(repoRoot(), 'config', filename);
      const content = fs.readFileSync(configPath, 'utf8');
      const match = content.match(/^\s*crew_home:\s*['"]?([^'"#\n]+?)['"]?\s*(?:#.*)?$/m);
      if (match?.[1]?.trim()) return match[1].trim();
    } catch {
      // Try the publishable example before using the standard local home.
    }
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
function resolveShellAllowedPath(rawPath: string, extraRoots: string[] = []): string {
  const resolved = path.resolve(rawPath);
  if (!path.isAbsolute(resolved)) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: path must be absolute`);
  }
  const taskWorkspaceRoot = getTaskWorkspaceRoot();
  const allowedRoots = [
    app.getPath('userData'),
    app.getPath('downloads'),
    app.getPath('documents'),
    app.getPath('pictures'),
    app.getPath('desktop'),
    taskWorkspaceRoot,
    path.dirname(taskWorkspaceRoot),
    ...extraRoots,
  ].map((r) => path.resolve(r));
  const allowed = allowedRoots.some((root) => resolved === root || resolved.startsWith(`${root}${path.sep}`));
  if (!allowed) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: path not under any allowed root`);
  }
  return resolved;
}

const MANAGED_GATEWAY_PORT = 8000;
const MANAGED_GATEWAY_URL = `http://127.0.0.1:${MANAGED_GATEWAY_PORT}`;
const AUTOSTART_ARG = '--autostart';
const IS_DEV_LAUNCH = process.argv.includes('--dev');
const RENDERER_LAUNCH_SEARCH = `?launchMode=${IS_DEV_LAUNCH ? 'dev' : 'account'}`;
const gatewayIdentityMode: GatewayIdentityMode = resolveGatewayIdentityMode(IS_DEV_LAUNCH);

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

function currentBrowserOwnerId(): string | null {
  return desktopAuthSession.ownerAccountId();
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
    const accessToken = gatewayInstanceAccessToken(activeGatewayCrewHome());
    const sessionCookie = desktopAuthSession.cookieHeader();
    const socket = new WebSocket(target.toString(), {
      headers: {
        Authorization: `Bearer ${accessToken}`,
        ...(sessionCookie ? { Cookie: sessionCookie } : {}),
      },
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

function resetRendererRevealGate(): void {
  rendererInitialStateReady = false;
  if (rendererReadyFallbackTimer) clearTimeout(rendererReadyFallbackTimer);
  rendererReadyFallbackTimer = setTimeout(() => {
    rendererReadyFallbackTimer = null;
    rendererInitialStateReady = true;
    console.warn('[main] renderer auth readiness timed out; showing fallback UI');
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
  windowShowRequested = true;
  revealMainWindowIfReady();
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
 * Resolve the BrowserWindow backgroundColor for cold start. Mirrors the logic
 * of the inline <head> script in index.html
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
  return isDark ? '#0f1115' : '#ffffff';
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
  resetRendererRevealGate();
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
    show: false,
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

  mainWindow.loadFile(path.join(__dirname, '../assets/index.html'), {
    query: { launchMode: IS_DEV_LAUNCH ? 'dev' : 'account' },
  });
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  mainWindow.webContents.on('will-navigate', (event, url) => {
    const expected = path.join(__dirname, '../assets/index.html');
    if (!isTrustedRendererFileUrl(url, expected, RENDERER_LAUNCH_SEARCH)) event.preventDefault();
  });

  mainWindow.once('ready-to-show', () => {
    nativeWindowReady = true;
    if (launchHidden) {
      hideMainWindowToTray();
      return;
    }
    revealMainWindowIfReady();
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
    const expected = path.join(__dirname, '../assets/index.html');
    if (!isMainFrame || !isTrustedRendererFileUrl(url, expected, RENDERER_LAUNCH_SEARCH)) return;
    browserHost?.hidePanel();
    resetRendererRevealGate();
  });
  mainWindow.webContents.on('did-finish-load', () => {
    // Push current backend health status on load so the renderer knows immediately
    mainWindow?.webContents.send('backend:status', backendStatusPayload(backendConnected));
  });

  mainWindow.on('hide', () => browserHost?.hidePanel());
  mainWindow.on('show', () => {
    mainWindow?.webContents.send('browser-view:layout-invalidated');
  });
  mainWindow.on('closed', () => {
    if (rendererReadyFallbackTimer) {
      clearTimeout(rendererReadyFallbackTimer);
      rendererReadyFallbackTimer = null;
    }
    browserHost?.hidePanel();
    mainWindow = null;
  });
  mainWindow.on('close', (event) => {
    if (isQuitting) return;
    if (!mainWindow) return;
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

/** Browser control is privileged even on loopback; bind it to this Gateway instance. */
function gatewayAccessHeaders(pathname: string): Record<string, string> {
  const headers: Record<string, string> = {};
  const sessionCookie = desktopAuthSession.cookieHeader();
  if (sessionCookie) headers.Cookie = sessionCookie;
  if (pathname.startsWith('/api/browser/')) {
    headers.Authorization = `Bearer ${gatewayInstanceAccessToken(activeGatewayCrewHome())}`;
  }
  return headers;
}

/**
 * 🌟 启动优化：加 2s AbortController 超时，避免半启动状态的 gateway 接受 TCP
 * 但无法返回 HTTP 响应时 fetch 无限挂起。
 */
async function hasHealthApi(baseUrl: string): Promise<boolean> {
  return (await probeHealthApi(baseUrl)).verified;
}

async function probeHealthApi(baseUrl: string) {
  return probeGatewayInstance(baseUrl, { crewHome: activeGatewayCrewHome() });
}

// 供开发态 / 回退使用的 Gateway
function startManagedGateway(): void {
  if (managedGateway) return;
  const root = repoRoot();
  const python = candidatePython();
  const crewHome = activeGatewayCrewHome();
  const env = {
    ...process.env,
    CREW_HOME: crewHome,
    GATEWAY_PORT: String(MANAGED_GATEWAY_PORT),
    PYTHONPATH: root,
    ...managedGatewayModeEnv(
      gatewayIdentityMode,
      crewHome,
    ),
    // Windows 控制台默认 GBK：强制 UTF-8，避免 Rich 日志写 emoji 时 UnicodeEncodeError 刷屏
    PYTHONIOENCODING: 'utf-8',
    ...(process.platform === 'win32' ? { PYTHONUTF8: '1' } : {}),
    // 不注入 CREW_TASK_WORKSPACE_ROOT，让后端从 config.yaml 自行计算
  };
  managedGateway = spawn(python, ['-m', 'crew.gateway.server'], {
    cwd: root,
    env,
    windowsHide: true,
  });
  const child = managedGateway;
  attachGatewayLog(child);
  writeGatewayLogLine(`[spawn] managed gateway pid=${child.pid} port=${MANAGED_GATEWAY_PORT} python=${python}`);
  child.stdout.on('data', (chunk) => console.log('[gateway]', String(chunk).trim()));
  child.stderr.on('data', (chunk) => console.warn('[gateway]', String(chunk).trim()));
  child.on('exit', (code, signal) => {
    console.warn('[gateway] exited', { code, signal });
    if (managedGateway === child) {
      managedGateway = null;
      ensureGatewayPromise = null;
      logSupervisorDecision('instance-exit', { platform: 'managed', code, signal });
      if (!isQuitting) gatewayRestartController.schedule();
    }
  });
  child.on('error', (error) => {
    console.error('[gateway] managed gateway process error:', error);
    if (managedGateway === child) {
      managedGateway = null;
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

  // 清理可能残留的僵尸 gateway 进程（上次 Electron 异常退出未清理）
  const killStart = Date.now();
  killZombieGatewayProcesses();
  console.log(`[gateway] Zombie cleanup took ${Date.now() - killStart}ms`);

  try {
    const spawnStart = Date.now();
    managedGateway = spawn(gatewayExePath, [], {
      cwd: gatewayDir,
      windowsHide: true,
      detached: false,
      // 不注入 CREW_TASK_WORKSPACE_ROOT，让后端从 config.yaml 自行计算
      env: {
        ...process.env,
        CREW_HOME: resolveCrewHome(),
        GATEWAY_PORT: String(port),
        // 与开发态一致：打包 exe 内嵌 Python 仍可能走 GBK 控制台
        PYTHONIOENCODING: 'utf-8',
        ...(process.platform === 'win32' ? { PYTHONUTF8: '1' } : {}),
      },
    });
    const child = managedGateway;
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
        ensureGatewayPromise = null;
        logSupervisorDecision('instance-exit', { platform: 'win32', code, signal });
        if (!isQuitting) gatewayRestartController.schedule();
      }
    });
    child.on('error', (err) => {
      console.error('[gateway] Failed to start Windows packaged gateway:', err);
      if (managedGateway === child) {
        managedGateway = null;
        ensureGatewayPromise = null;
        logSupervisorDecision('instance-error', { platform: 'win32', error: String(err) });
        if (!isQuitting) gatewayRestartController.schedule();
      }
    });
  } catch (err) {
    console.error('[gateway] Exception while spawning Windows gateway:', err);
  }
}

/**
 * macOS 打包版专用：在指定端口启动 crew-gateway 二进制。
 * macOS .app 结构：Crew.app/Contents/Resources/crew-gateway/
 */
function startMacOSPackagedGateway(port: number): void {
  if (managedGateway) return;

  // macOS .app bundle: exe 位于 Contents/MacOS/crew-desktop，
  // gateway 放在 Contents/Resources/crew-gateway/crew-gateway
  const resourcesPath = path.join(path.dirname(app.getPath('exe')), '..', 'Resources');
  const gatewayExePath = path.join(resourcesPath, 'crew-gateway', 'crew-gateway');
  const gatewayDir = path.dirname(gatewayExePath);

  console.log(`[gateway] Starting packaged macOS gateway on port ${port}:`, gatewayExePath);

  // 注意：不在此处调用 killZombieGatewayProcesses()。
  // macOS 上 killZombieGatewayProcesses 使用 `pkill -f crew-gateway`，
  // 它会匹配命令行中包含 "crew-gateway" 的所有进程——包括本函数刚刚 spawn
  // 出来的新 gateway，导致新进程被 SIGTERM 误杀（pkill 是异步的，无法按 PID 排除）。
  // 僵尸进程清理统一在 before-quit / uninstall 时执行即可。

  try {
    managedGateway = spawn(gatewayExePath, [], {
      cwd: gatewayDir,
      detached: false,
      env: {
        ...process.env,
        CREW_HOME: resolveCrewHome(),
        GATEWAY_PORT: String(port),
      },
    });
    const child = managedGateway;

    attachGatewayLog(child);
    writeGatewayLogLine(`[spawn] packaged mac gateway pid=${child.pid} port=${port} exe=${gatewayExePath}`);
    child.stdout.on('data', (chunk) => console.log('[gateway-mac]', String(chunk).trim()));
    child.stderr.on('data', (chunk) => console.warn('[gateway-mac]', String(chunk).trim()));

    child.on('exit', (code, signal) => {
      console.warn('[gateway] macOS packaged gateway exited', { code, signal });
      if (managedGateway === child) {
        managedGateway = null;
        ensureGatewayPromise = null;
        logSupervisorDecision('instance-exit', { platform: 'darwin', code, signal });
        if (!isQuitting) gatewayRestartController.schedule();
      }
    });
    child.on('error', (err) => {
      console.error('[gateway] Failed to start macOS packaged gateway:', err);
      if (managedGateway === child) {
        managedGateway = null;
        ensureGatewayPromise = null;
        logSupervisorDecision('instance-error', { platform: 'darwin', error: String(err) });
        if (!isQuitting) gatewayRestartController.schedule();
      }
    });
  } catch (err) {
    console.error('[gateway] Exception while spawning macOS gateway:', err);
  }
}

/**
 * Linux 打包版专用：在指定端口启动 crew-gateway 二进制。
 * deb 装在固定路径 /opt/crew-gateway/crew-gateway。
 *
 * 与 macOS/Windows 一致，由 desktop 子进程托管 gateway：启动时 spawn，退出时
 * 随之结束，避免多用户常驻服务争抢端口或使用不一致的 instance key。
 */
function startLinuxPackagedGateway(port: number): void {
  if (managedGateway) return;

  const gatewayExePath = '/opt/crew-gateway/crew-gateway';
  const gatewayDir = path.dirname(gatewayExePath);

  console.log(`[gateway] Starting packaged Linux gateway on port ${port}:`, gatewayExePath);

  try {
    managedGateway = spawn(gatewayExePath, [], {
      cwd: gatewayDir,
      detached: false,
      env: {
        ...process.env,
        CREW_HOME: resolveCrewHome(),
        GATEWAY_PORT: String(port),
      },
    });
    const child = managedGateway;

    attachGatewayLog(child);
    writeGatewayLogLine(`[spawn] packaged linux gateway pid=${child.pid} port=${port} exe=${gatewayExePath}`);
    child.stdout.on('data', (chunk) => console.log('[gateway-linux]', String(chunk).trim()));
    child.stderr.on('data', (chunk) => console.warn('[gateway-linux]', String(chunk).trim()));

    child.on('exit', (code, signal) => {
      console.warn('[gateway] Linux packaged gateway exited', { code, signal });
      if (managedGateway === child) {
        managedGateway = null;
        ensureGatewayPromise = null;
        logSupervisorDecision('instance-exit', { platform: 'linux', code, signal });
        if (!isQuitting) gatewayRestartController.schedule();
      }
    });
    child.on('error', (err) => {
      console.error('[gateway] Failed to start Linux packaged gateway:', err);
      if (managedGateway === child) {
        managedGateway = null;
        ensureGatewayPromise = null;
        logSupervisorDecision('instance-error', { platform: 'linux', error: String(err) });
        if (!isQuitting) gatewayRestartController.schedule();
      }
    });
  } catch (err) {
    console.error('[gateway] Exception while spawning Linux gateway:', err);
  }
}

/**
 * 清理可能残留的僵尸 gateway 进程。
 * 上次 Electron 异常退出时 managedGateway.kill() 可能未执行，
 * 导致旧 gateway 进程占着 8000 端口，新 gateway 无法启动。
 */
function killZombieGatewayProcesses(): void {
  if (process.platform === 'win32') {
    try {
      // 查找所有 crew-gateway.exe 进程（排除当前 managedGateway）
      const output = spawn('tasklist', ['/FI', 'IMAGENAME eq crew-gateway.exe', '/FO', 'CSV', '/NH'], {
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'ignore'],
      });

      let stdout = '';
      output.stdout.on('data', (chunk) => { stdout += chunk; });

      output.on('close', () => {
        const lines = stdout.split('\n').filter(line => line.includes('crew-gateway.exe'));
        for (const line of lines) {
          const match = line.match(/"crew-gateway\.exe","(\d+)"/);
          if (match) {
            const pid = parseInt(match[1], 10);
            // 跳过当前管理的 gateway（如果存在）
            if (managedGateway && managedGateway.pid === pid) continue;

            console.warn(`[gateway] Killing zombie crew-gateway process: PID ${pid}`);
            try {
              spawn('taskkill', ['/PID', String(pid), '/F'], {
                windowsHide: true,
                stdio: 'ignore',
              });
            } catch (err) {
              console.error(`[gateway] Failed to kill PID ${pid}:`, err);
            }
          }
        }
      });
    } catch (err) {
      console.warn('[gateway] Failed to enumerate gateway processes:', err);
    }
  } else if (process.platform === 'linux') {
    try {
      // Linux 打包态 gateway 由本进程 spawn 托管（不再用 systemd user service）。
      // 只杀当前用户的残留 crew-gateway，避免误杀别的用户各自的 gateway 实例。
      spawn('pkill', ['-u', String(process.getuid?.() ?? 0), '-f', '/opt/crew-gateway/crew-gateway'], {
        stdio: 'ignore',
      });
    } catch (err) {
      console.warn('[gateway] Failed to kill Linux gateway processes:', err);
    }
  } else if (process.platform === 'darwin') {
    try {
      spawn('pkill', ['-f', 'crew-gateway'], { stdio: 'ignore' });
    } catch (err) {
      console.warn('[gateway] Failed to kill macOS gateway processes:', err);
    }
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
    fs.mkdirSync(path.dirname(file), { recursive: true });
    gatewayLogStream?.end();
    gatewayLogStream = fs.createWriteStream(file, { flags: 'w', encoding: 'utf8' });
    const write = (prefix: string, chunk: Buffer | string): void => {
      const text = String(chunk);
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
  } catch (err) {
    console.error('[gateway] failed to attach startup log:', err);
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
    if (await hasHealthApi(baseUrl)) {
      console.log(`[gateway] Health API ready after ${Date.now() - started}ms (${attempts} attempts)`);
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
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
  // Use the resolved gateway base URL (updated by ensureGateway after port selection)
  const baseUrl = resolvedGatewayBaseUrl;
  const probe = await probeHealthApi(baseUrl);
  if (probe.verified) {
    // 恢复要快：一旦成功立即重置并推送 connected（若先前被判 disconnected，遮罩立刻消失）
    healthFailCount = 0;
    const componentsChanged = JSON.stringify(gatewayComponents) !== JSON.stringify(probe.components);
    gatewayComponents = probe.components;
    pushBackendStatus(true, { force: componentsChanged });
    return;
  }
  // 失败容错：连续 N 次失败才判 disconnected。gateway 繁忙时单次 health 超时属正常，
  // 立即弹遮罩会误阻断用户操作（如点技能时 gateway 正在加载）。
  healthFailCount += 1;
  if (healthFailCount >= HEALTH_FAIL_THRESHOLD) {
    gatewayComponents = undefined;
    pushBackendStatus(false);
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
}

// ============================================================================
// 🌟 核心改造：双平台极致适配的 Gateway 路由
// ============================================================================
async function ensureGateway(): Promise<{ baseUrl: string; managed: boolean }> {
  if (ensureGatewayPromise) {
    const cached = await ensureGatewayPromise;
    // A prior proof is not a permanent trust grant. Re-prove immediately before
    // every credential-bearing caller reuses the URL, so an unmanaged Gateway
    // restart cannot silently turn a stale cached port into a trusted service.
    // 与 pollBackendHealth 容错对齐：gateway 单线程 asyncio 繁忙（摘要/工具执行）+
    // Defender 扫描时，单次 3s health 超时属正常；连续失败才判定实例失效。
    // 否则一次抖动就清空缓存 → 全量重拉 → 旧实例占 8000 → 扫描选出 8001 →
    // spawn 被 managedGateway 短路 → 空等无进程端口，遮罩永不消失。
    for (let attempt = 0; attempt < HEALTH_FAIL_THRESHOLD; attempt++) {
      if (await hasHealthApi(cached.baseUrl)) return cached;
      await new Promise((r) => setTimeout(r, 300));
    }
    // 只在自己仍是缓存持有方时清除，避免误清 retry 已设置的新代际 promise。
    // ensureGatewayPromise 类型为 Promise，await 后是结果对象；这里直接置空即可（
    // retry 会重新设置，不会读到 null）。
    ensureGatewayPromise = null;
    throw new Error('previously verified Crew Gateway no longer proves its instance identity');
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

    // 真实账号可复用外部 Gateway；dev 兜底必须使用带 dev_mode 的托管实例。
    // Linux 打包态除外：由本进程 spawn 托管，不复用外部 gateway（避免复用到别的
    // 用户的 gateway 导致 instance key 验签失败）。
    if (
      shouldProbeExternalGateway(gatewayIdentityMode)
      && !(app.isPackaged && (process.platform === 'win32' || process.platform === 'linux'))
    ) {
      if (await hasHealthApi(DEFAULT_GATEWAY_URL)) {
        resolvedGatewayBaseUrl = DEFAULT_GATEWAY_URL;
        return { baseUrl: DEFAULT_GATEWAY_URL, managed: false };
      }
    }

    // 3. Linux 打包版由 desktop 子进程托管 gateway（与 Win/mac 一致），
    // 让进程生命周期与桌面端一致，并避免多用户实例争抢端口或验签配置不一致。
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
      // spawn 前暴露真实端口，wait 期间监控打对地址；无冷启动天花板，靠 child 退出兜底。
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

    // 4. 开发环境 / 兜底：拉起 MANAGED_GATEWAY_PORT 端口的 Python 子进程
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
  if (!isTrustedRendererFileUrl(senderFrame.url, expectedFile, RENDERER_LAUNCH_SEARCH)) {
    throw new Error(`${IPC_ARG_VALIDATION_FAILED}: IPC sender must be the packaged renderer`);
  }
}

const rawIpcHandle = ipcMain.handle.bind(ipcMain);

function trustedHandle(channel: string, listener: TrustedIpcHandler): void {
  rawIpcHandle(channel, (event, ...args) => {
    assertTrustedRenderer(event);
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
    if (downloaded) preserved.add(path.resolve(downloaded.filePath));
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
async function installDownloadedUpdate(): Promise<VersionUpdatePackageResult> {
  const downloaded = readUpdateState().downloaded;
  if (!downloaded) {
    return { success: false, message: '没有已下载的更新包' };
  }

  const integrity = verifyPackageIntegrity(downloaded.filePath, downloaded.size);
  if (!integrity.ok) {
    sendVersionUpdateDownloadProgress({ phase: 'error', message: integrity.message });
    // 校验失败的包不可用，清掉状态（文件留给 cleanup 兜底）
    setDownloadedRecord(null);
    return { success: false, message: integrity.message };
  }

  const targetPath = path.resolve(downloaded.filePath);
  const quitAfterLaunch = () => {
    setTimeout(() => {
      isQuitting = true;
      app.quit();
    }, 500);
  };

  sendVersionUpdateDownloadProgress({ phase: 'installing', percent: 100 });
  try {
    const ext = path.extname(targetPath).toLowerCase();
    if (process.platform === 'win32' && ext === '.exe') {
      // Inno Setup 静默安装；/NORESTART 避免安装器替我们重启。
      // 安装器由下游发行流水线生成，不属于当前源码预览版。
      const child = spawn(targetPath, ['/SILENT', '/NORESTART'], { detached: true, stdio: 'ignore', windowsHide: true });
      child.unref();
      setDownloadedRecord(null); // 包已消费
      sendVersionUpdateDownloadProgress({ phase: 'completed', percent: 100 });
      quitAfterLaunch();
      return { success: true, message: '安装程序已启动' };
    }

    if (process.platform === 'linux' && ext === '.deb') {
      await fs.promises.chmod(targetPath, 0o644);
      const usePkexec = typeof process.getuid === 'function' && process.getuid() !== 0;
      const command = usePkexec ? 'pkexec' : 'dpkg';
      const args = usePkexec ? ['dpkg', '-i', targetPath] : ['-i', targetPath];
      const child = spawn(command, args, { detached: true, stdio: 'ignore' });
      child.unref();

      // 装完自动重启 GUI（对标 Windows 的 Inno postinstall）。
      // 必须用独立 helper、不能放 deb postinst：postinst 以 root 跑，缺 DISPLAY / XAUTHORITY /
      // DBUS_SESSION_BUS_ADDRESS，拉不起用户会话 GUI；helper 继承当前 app 的用户会话 env。
      //
      // gateway 不再用 systemd user service（改由 desktop 子进程托管），故此处不
      // systemctl restart。等旧 app 退出后杀掉它 spawn 的旧 gateway（避免占着 8000
      // 让新 desktop 扫到别的端口），再重启 desktop——新 desktop 的 ensureGateway 会
      // spawn 新二进制的 gateway。
      // ponytail 边界（出问题先查这里）：
      //   1. pkexec 被取消 / dpkg 失败 → deb 没装上，但 helper 仍会重启 GUI。兜底：GUI 起来后
      //      心跳重新比对版本，forceLock 再次阻断 / reminder 再次提示，不会“假装更新成功”。
      //   2. crew-desktop 须在 PATH 上（继承自 app 的用户会话 PATH）；若不全则改绝对路径。
      // helper 参数：$1 = 安装进程 pid（pkexec/dpkg），$2 = 当前 app 主进程 pid。
      const installPid = child.pid;
      if (installPid) {
        const restarter = [
          'n=0; while kill -0 "$1" 2>/dev/null && [ $n -lt 120 ]; do sleep 0.5; n=$((n+1)); done', // 等安装流程结束（≤60s）
          'n=0; while kill -0 "$2" 2>/dev/null && [ $n -lt 40 ]; do sleep 0.3; n=$((n+1)); done',  // 等旧 app 退出（≤12s）
          'sleep 1', // dpkg 换完文件后收尾
          'pkill -u "$(id -u)" -f /opt/crew-gateway/crew-gateway 2>/dev/null', // 清旧 gateway
          'sleep 1', // 等端口释放
          'exec crew-desktop',
        ].join('; ');
        spawn(
          'sh',
          ['-c', restarter, 'crew-restarter', String(installPid), String(process.pid)],
          { detached: true, stdio: 'ignore' },
        ).unref();
      }

      setDownloadedRecord(null);
      sendVersionUpdateDownloadProgress({ phase: 'completed', percent: 100 });
      quitAfterLaunch();
      return { success: true, message: '安装程序已启动' };
    }

    if (process.platform === 'darwin' && ext === '.dmg') {
      const child = spawn('open', [targetPath], { detached: true, stdio: 'ignore' });
      child.unref();
      sendVersionUpdateDownloadProgress({ phase: 'completed', percent: 100 });
      quitAfterLaunch();
      return { success: true, message: '安装包已打开' };
    }

    const openError = await shell.openPath(targetPath);
    if (openError) {
      sendVersionUpdateDownloadProgress({ phase: 'error', message: openError });
      return { success: false, message: openError };
    }
    sendVersionUpdateDownloadProgress({ phase: 'completed', percent: 100 });
    quitAfterLaunch();
    return { success: true, message: '安装包已打开' };
  } catch (error) {
    // 安装启动失败：保留 downloaded，用户可再次点击安装（复用已下载包）
    sendVersionUpdateDownloadProgress({ phase: 'error', message: (error as Error).message });
    return { success: false, message: (error as Error).message || '启动安装失败' };
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
    if (!fs.existsSync(state.downloaded.filePath)) {
      // 文件已不在（被清理 / 手动删除）→ 清状态
      setDownloadedRecord(null);
    } else {
      // 推送 downloaded 态：渲染层恢复为「可安装」
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
    case 'pdf': return 'application/pdf';
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
  trustedHandle('app:quit', () => {
    isQuitting = true;
    app.quit();
  });
  trustedHandle('app:get-version', () => ({
    version: currentAppVersion(app),
    label: currentAppVersionLabel(app),
  }));
  ipcMain.on('window:maximized', (e) => {
    assertTrustedRenderer(e);
    e.sender.send('window:maximized-changed', true);
  });
  ipcMain.on('window:unmaximized', (e) => {
    assertTrustedRenderer(e);
    e.sender.send('window:maximized-changed', false);
  });

  trustedHandle('shell:openExternal', (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenExternalArgs.parse(raw), 'shell:openExternal');
    return shell.openExternal(args.url);
  });

  trustedHandle('shell:openPath', (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'shell:openPath');
    const extraRoots = args.allowedRoot ? [args.allowedRoot] : [];
    const resolved = resolveShellAllowedPath(args.path, extraRoots);
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
    const resolved = resolveShellAllowedPath(args.path);
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
    const resolved = resolveShellAllowedPath(args.path);
    const stat = await fs.promises.stat(resolved).catch(() => null);
    const maxBytes = 2 * 1024 * 1024;
    const bytes = Buffer.byteLength(args.content, 'utf8');
    if (stat && !stat.isFile()) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:writeTextFile target is not a file`);
    }
    if (bytes > maxBytes) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:writeTextFile file too large`);
    }
    await fs.promises.writeFile(resolved, args.content, 'utf8');
    return { ok: true };
  });

  ipcMain.handle('shell:readFileBase64', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'shell:readFileBase64');
    const resolved = resolveShellAllowedPath(args.path);
    const stat = await fs.promises.stat(resolved);
    const maxBytes = 50 * 1024 * 1024;
    if (!stat.isFile()) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:readFileBase64 target is not a file`);
    }
    if (stat.size > maxBytes) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:readFileBase64 file too large`);
    }
    const ext = path.extname(resolved).toLowerCase();
    const mimeTypes: Record<string, string> = {
      '.pdf': 'application/pdf',
      '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
      '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      '.png': 'image/png',
      '.jpg': 'image/jpeg',
      '.jpeg': 'image/jpeg',
      '.gif': 'image/gif',
      '.webp': 'image/webp',
      '.bmp': 'image/bmp',
      '.ico': 'image/x-icon',
      '.tif': 'image/tiff',
      '.tiff': 'image/tiff',
    };
    const buffer = await fs.promises.readFile(resolved);
    return {
      base64: buffer.toString('base64'),
      mimeType: mimeTypes[ext] || 'application/octet-stream',
    };
  });

  trustedHandle('shell:writeFileBase64', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellWriteFileBase64Args.parse(raw), 'shell:writeFileBase64');
    const resolved = resolveShellAllowedPath(args.path);
    const stat = await fs.promises.stat(resolved).catch(() => null);
    const buffer = Buffer.from(args.base64, 'base64');
    const maxBytes = 50 * 1024 * 1024;
    if (stat && !stat.isFile()) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:writeFileBase64 target is not a file`);
    }
    if (buffer.byteLength > maxBytes) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:writeFileBase64 file too large`);
    }
    await fs.promises.writeFile(resolved, buffer);
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

  trustedHandle('shell:showItemInFolder', (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'shell:showItemInFolder');
    const resolved = resolveShellAllowedPath(args.path);
    shell.showItemInFolder(resolved);
  });

  trustedHandle('shell:listOpenApplications', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathArgs.parse(raw), 'shell:listOpenApplications');
    const resolved = resolveShellAllowedPath(args.path);
    const stat = await fs.promises.stat(resolved);
    if (!stat.isFile()) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:listOpenApplications target is not a file`);
    }
    return listOpenWithApplications(resolved);
  });

  trustedHandle('shell:openPathWith', async (_e, raw: unknown) => {
    const args = parseOrThrow(ShellOpenPathWithArgs.parse(raw), 'shell:openPathWith');
    const resolved = resolveShellAllowedPath(args.path);
    const stat = await fs.promises.stat(resolved);
    if (!stat.isFile()) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: shell:openPathWith target is not a file`);
    }
    await openFileWithApplication(resolved, args.applicationId);
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
    return r.filePaths;
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

  trustedHandle('auth:get-state', async () => {
    const ensured = await ensureGateway();
    return desktopAuthSession.refreshConfig(ensured.baseUrl);
  });
  trustedHandle('auth:send-code', async (_e, raw: unknown) => {
    const phoneNumber = (
      raw && typeof raw === 'object' && typeof (raw as Record<string, unknown>).phoneNumber === 'string'
        ? String((raw as Record<string, unknown>).phoneNumber)
        : ''
    ).trim();
    if (!phoneNumber || phoneNumber.length > 32) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid phoneNumber`);
    }
    const ensured = await ensureGateway();
    await desktopAuthSession.refreshConfig(ensured.baseUrl);
    return desktopAuthSession.sendCode(ensured.baseUrl, phoneNumber);
  });
  trustedHandle('auth:login', async (_e, raw: unknown) => {
    const record = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {};
    const identifier = typeof record.identifier === 'string' ? record.identifier.trim() : '';
    const code = typeof record.code === 'string' ? record.code.trim() : '';
    if (!identifier || identifier.length > 128 || code.length > 32) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid login input`);
    }
    const ensured = await ensureGateway();
    const state = await desktopAuthSession.refreshConfig(ensured.baseUrl);
    if (state.mode === 'remote' && !code) {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: invalid login input`);
    }
    const result = state.mode === 'email'
      ? await desktopAuthSession.loginWithEmail(ensured.baseUrl, identifier)
      : await desktopAuthSession.login(ensured.baseUrl, identifier, code);
    if (result.ok === true) {
      closeGatewaySockets('login-changed');
      await resetBrowserHost('login-changed');
      scheduleBrowserHostConnection();
      mainWindow?.webContents.send('auth:session-state', desktopAuthSession.state());
    }
    return result;
  });
  trustedHandle('auth:logout', async () => {
    const ensured = await ensureGateway();
    const result = await desktopAuthSession.logout(ensured.baseUrl);
    if (result.ok === true) {
      closeGatewaySockets('logout');
      await resetBrowserHost('logout');
      mainWindow?.webContents.send('auth:session-state', desktopAuthSession.state());
    }
    return result;
  });

  trustedHandle('update:start-download', async (_e, raw: unknown) => {
    const args = parseOrThrow(UpdateStartDownloadArgs.parse(raw), 'update:start-download');
    return startUpdateDownload({ version: args.version, type: args.type });
  });

  trustedHandle('update:pause', () => pauseUpdateDownload());
  trustedHandle('update:resume', () => resumeUpdateDownload());
  trustedHandle('update:retry', async (_e, raw: unknown) => {
    const args = parseOrThrow(UpdateStartDownloadArgs.parse(raw), 'update:retry');
    return retryUpdateDownload({ version: args.version, type: args.type });
  });

  trustedHandle('update:install-package', async () => installDownloadedUpdate());

  trustedHandle('update:get-state', (): UpdateStateSnapshot => readUpdateState());

  trustedHandle('feedback:submit', async (_e, raw: unknown) => {
    const args = parseOrThrow(FeedbackSubmitArgs.parse(raw), 'feedback:submit');
    return submitFeedback(args);
  });
  trustedHandle('feedback:list', async (_e, params: unknown) => {
    const validated = parseOrThrow(FeedbackListArgs.parse(params), 'feedback:list');
    return getFeedbackList(validated);
  });
  trustedHandle('feedback:image', async (_e, rawArgs: unknown) => {
    const validated = parseOrThrow(FeedbackImageArgs.parse(rawArgs), 'feedback:image');
    return getFeedbackImage(validated.path);
  });

  trustedHandle('gateway:fetch', async (_e, raw: unknown) => {
    const args = parseOrThrow(GatewayFetchArgs.parse(raw), 'gateway:fetch');
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
        'origin', 'referer', 'content-length',
      ]);
      const sanitized: Record<string, string> = {};
      const incoming = args.init.headers as Record<string, string>;
      for (const [key, value] of Object.entries(incoming)) {
        if (DENYLIST.has(String(key).toLowerCase())) continue;
        sanitized[key] = value;
      }
      fetchInit.headers = sanitized;
    }
    fetchInit.headers = {
      ...(fetchInit.headers as Record<string, string> | undefined),
      ...gatewayAccessHeaders(targetUrl.pathname),
    };
    if (args.init?.body !== undefined) fetchInit.body = args.init.body;
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
    const ensured = await ensureGateway();
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
    Object.assign(headers, gatewayAccessHeaders(targetUrl.pathname));
    const controller = new AbortController();
    gatewayStreamControllers.get(streamKey)?.abort();
    gatewayStreamControllers.set(streamKey, controller);
    try {
      const response = await fetch(targetUrl.toString(), {
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
      const decoder = new TextDecoder();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const text = decoder.decode(value, { stream: true });
        if (text) send({ type: 'chunk', text });
      }
      const tail = decoder.decode();
      if (tail) send({ type: 'chunk', text: tail });
      send({ type: 'end' });
      return { ok: response.ok };
    } catch (error) {
      if ((error as Error).name !== 'AbortError') {
        send({ type: 'error', error: (error as Error).message || 'Gateway 流请求失败' });
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
   * gateway:upload — Wiki 本地文件 multipart 上传通道。
   *
   * gateway:fetch 只透传 string body，无法承载二进制；这里由 renderer 传文件
   * 绝对路径，主进程读文件 + Node 内置 FormData/Blob 组 multipart POST 到
   * gateway。目标 path 精确白名单（GATEWAY_UPLOAD_ALLOWED_PATHS，当前仅
   * /api/wiki/upload），hostname 钳制与 gateway:fetch 一致。
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
    const ensured = await ensureGateway();
    const targetUrl = new URL(args.url);
    const ensuredUrl = new URL(ensured.baseUrl);
    targetUrl.protocol = ensuredUrl.protocol;
    targetUrl.hostname = ensuredUrl.hostname;
    targetUrl.port = ensuredUrl.port;
    const uploadUrl = targetUrl.toString();
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
        const res = await fetch(uploadUrl, {
          method: 'POST',
          headers: gatewayAccessHeaders(targetUrl.pathname),
          body: form,
        });
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

  trustedHandle('gateway:ensure', async () => ensureGateway());
  trustedHandle('gateway:get-status', () => backendStatusPayload(backendConnected));

  // 冷启动/卡死时 renderer「重试」按钮调用。
  // 协作式作废（generation++）+ 等旧实例真正退出后才重建，保证同一时刻至多一个
  // ensureGateway 流程，从根源上消除「旧实例未退 → 扫到新端口 → spawn 被短路 →
  // 空等无进程端口」的循环拉起。外部/systemd gateway 无 managedGateway，直接重建。
  trustedHandle('gateway:retry', async () => {
    gatewayGeneration += 1;
    logSupervisorDecision('user-retry', { generation: gatewayGeneration });
    ensureGatewayPromise = null;
    const gw = managedGateway;
    if (gw) {
      managedGateway = null;
      await new Promise<void>((resolve) => {
        const done = () => resolve();
        gw.once('exit', done);
        setTimeout(() => {
          try { gw.kill('SIGKILL'); } catch { /* already dead */ }
          setTimeout(done, 400);
        }, 3000);
        try { gw.kill(); } catch { /* already dead */ }
      });
      // 旧实例已被 retry 路径接管退出（exit handler 因 managedGateway=null 不会记录），
      // 这里显式补一条决策日志，保证重试→旧实例退出在 gateway-startup.log 可回溯（B5）。
      logSupervisorDecision('instance-exit-superseded', { pid: gw.pid ?? -1 });
    }
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

    const ensured = await ensureGateway();
    if (gatewaySocketGenerations.get(senderId) !== generation || event.sender.isDestroyed()) {
      return { ok: false, error: 'WebSocket 连接已取消' };
    }
    const httpUrl = new URL(ensured.baseUrl);
    httpUrl.protocol = httpUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    httpUrl.pathname = '/ws';
    httpUrl.search = '';
    httpUrl.hash = '';

    const sessionCookie = desktopAuthSession.cookieHeader();
    const socket = new WebSocket(httpUrl.toString(), {
      headers: sessionCookie ? { Cookie: sessionCookie } : {},
    });
    gatewaySockets.set(senderId, socket);
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
        reason: reason.toString(),
      });
      if (gatewaySockets.get(senderId) === socket) gatewaySockets.delete(senderId);
    });
    socket.on('error', (err) => {
      sendEvent({ type: 'error', error: err.message });
    });
    event.sender.once('destroyed', handleRendererDestroyed);
    return { ok: true };
  });

  trustedHandle('gateway-ws:send', (event, payload: unknown) => {
    const socket = gatewaySockets.get(event.sender.id);
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return { ok: false, error: 'WebSocket 未连接' };
    }
    let data = '';
    try { data = typeof payload === 'string' ? payload : JSON.stringify(payload ?? {}); } catch {
      throw new Error(`${IPC_ARG_VALIDATION_FAILED}: gateway WebSocket payload must be serializable`);
    }
    if (Buffer.byteLength(data, 'utf8') > 4 * 1024 * 1024) {
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
    const ensured = await ensureGateway();
    if (browserSocketGenerations.get(senderId) !== generation || event.sender.isDestroyed()) {
      return { ok: false, error: '浏览器状态连接已取消' };
    }
    const target = new URL(ensured.baseUrl);
    target.protocol = target.protocol === 'https:' ? 'wss:' : 'ws:';
    target.pathname = `/ws/browser/${encodeURIComponent(sessionId)}`;
    target.search = '';
    target.hash = '';
    const accessToken = gatewayInstanceAccessToken(activeGatewayCrewHome());
    const sessionCookie = desktopAuthSession.cookieHeader();
    const socket = new WebSocket(target.toString(), {
      headers: {
        Authorization: `Bearer ${accessToken}`,
        ...(sessionCookie ? { Cookie: sessionCookie } : {}),
      },
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
      sendEvent({ type: 'close', sessionId, code, reason: reason.toString() });
      browserSockets.delete(senderId);
    });
    socket.on('error', (error) => sendEvent({ type: 'error', sessionId, error: error.message }));
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
        error: (error instanceof Error ? error.message : '无法挂载浏览器页面').slice(0, 500),
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
        error: (error instanceof Error ? error.message : '无法读取浏览器导航状态').slice(0, 500),
      };
    }
  });

}

async function bootstrap() {
  await app.whenReady();
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
  // 非打包态：同样后台启动 managed Gateway，避免 browser-host
  // 连到 8000 端口的残留 Gateway 或空端口。
  if (IS_DEV_LAUNCH) {
    // 开发态统一使用托管 Gateway 端口，避免默认 8000 上残留进程导致
    // browser-host 连接错误。
    process.env.GATEWAY_PORT = String(MANAGED_GATEWAY_PORT);
  }

  // 🌟 关键优化：健康监控立即启动，不等 ensureGateway 完成
  // 这样渲染进程能实时收到 backend:status 推送，不再卡在 loading
  startBackendHealthMonitor();

  // 后台异步启动 Gateway（不阻塞窗口创建）
  ensureGateway()
    .then(async result => {
      console.log('[main] Gateway started:', result);
      try {
        const resolvedPort = new URL(result.baseUrl).port;
        if (resolvedPort) {
          process.env.GATEWAY_PORT = resolvedPort;
        }
      } catch {
        /* ignore malformed URL */
      }
      try {
        await desktopAuthSession.refreshConfig(result.baseUrl);
      } catch (error) {
        console.warn('[auth] failed to load gateway auth config:', (error as Error).message);
      }
      scheduleBrowserHostConnection();
    })
    .catch(err => {
      console.error('[main] Gateway start failed:', err);
      // 进程可能仍在慢启动：保持健康监控，不在此处 push disconnected。
    });

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
    stopManagedGateway: (timeoutMs = 3000) => {
      return new Promise((resolve) => {
        if (!managedGateway) return resolve();
        const gw = managedGateway;
        const done = () => {
          if (managedGateway === gw) managedGateway = null;
          resolve();
        };
        gw.once('exit', done);
        const timer = setTimeout(() => {
          try { gw.kill('SIGKILL'); } catch { /* ignore */ }
          setTimeout(done, 500);
        }, timeoutMs);
        try { gw.kill(); } catch { /* already dead */ }
        gw.once('exit', () => clearTimeout(timer));
      });
    },
    killZombieGatewayProcesses,
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
  if (process.platform !== 'darwin') app.quit();
});

// 优雅关闭：包含网络释放以及后台猎杀
app.on('before-quit', () => {
  isQuitting = true;
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
  // 🌟 新增：彻底猎杀 Electron 托管的 Python 后台进程，防止驻留
  if (managedGateway) {
    console.log('[main] Killing managed gateway process before quit...');
    managedGateway.kill();
    managedGateway = null;
  }
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
