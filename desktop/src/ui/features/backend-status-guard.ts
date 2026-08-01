/**
 * 后端服务状态守卫 —— 全局 UI 拦截与 Loading 遮罩。
 *
 * 监听主进程推送的 `backend:status` IPC 事件（周期健康检查 /api/health），
 * 当后端不可用时展示全局遮罩阻断用户操作，恢复后自动隐藏。
 *
 * 本守卫只负责本地 Gateway 可用性，不承担用户身份判断。
 *
 * 慢启动容错：遮罩持续超过 SLOW_THRESHOLD_MS 后，从「光秃秃转圈」升级为
 * 「仍在准备中（已等 Ns）+ 查看日志 / 重试 / 继续等待」，把无限静默转圈变成
 * 用户可动手的转圈（诊断 AV 卡 cacert / gateway 崩溃 traceback / 端口冲突等）。
 */

import type { BackendChatSocket } from '../backend-client';
import { notify } from '../state';
import { uiStore } from '../stores/stores';

const OVERLAY_ID = 'backend-loading-overlay';
const ELAPSED_ID = 'backend-loading-elapsed';
const ACTIONS_ID = 'backend-loading-actions';
const LOG_BTN_ID = 'backend-loading-log';
const RETRY_BTN_ID = 'backend-loading-retry';
const DISMISS_BTN_ID = 'backend-loading-dismiss';
/** 超过此阈值仍连不上，就升级为「仍在准备中」+ 操作按钮。 */
const SLOW_THRESHOLD_MS = 20_000;
const SLOW_TICK_MS = 1000;

let initialized = false;
let overlayEl: HTMLElement | null = null;
/** init 期间允许 setTab 绕过守卫，确保遮罩下方的 UI 骨架正常构建。 */
let initBypassActive = true;
/** 避免 health 抖动时重复触发恢复 hydrate。 */
let recoverInFlight = false;

// ── 慢启动计时 ──
let slowTimer: number | null = null;
let overlayShownAt = 0;
let currentLogPath = '';
/** 用户点了「继续等待」后本轮不再弹操作按钮，直到下次 disconnected 周期。 */
let slowDismissed = false;
let buttonsBound = false;
let lastComponentWarning = '';

function resolveOverlay(): HTMLElement | null {
  if (overlayEl) return overlayEl;
  overlayEl = document.getElementById(OVERLAY_ID);
  return overlayEl;
}

/**
 * 根据当前后端连接状态更新遮罩可见性。
 * connected=true → 隐藏遮罩；connected=false → 展示遮罩。
 */
function applyBackendOverlay(connected: boolean): void {
  const el = resolveOverlay();
  if (!el) return;
  if (connected) {
    el.style.display = 'none';
    stopSlowTimer();
  } else {
    el.style.display = '';
    // Remove `hidden` attribute if present so display style takes effect
    el.removeAttribute('hidden');
    startSlowTimer();
  }
}

function startSlowTimer(): void {
  if (slowTimer !== null) return;
  overlayShownAt = Date.now();
  slowDismissed = false;
  hideSlowActions();
  slowTimer = window.setInterval(tickSlow, SLOW_TICK_MS);
}

function stopSlowTimer(): void {
  if (slowTimer !== null) {
    window.clearInterval(slowTimer);
    slowTimer = null;
  }
  hideSlowActions();
}

function tickSlow(): void {
  const elapsed = Date.now() - overlayShownAt;
  if (elapsed < SLOW_THRESHOLD_MS || slowDismissed) return;
  const elapsedEl = document.getElementById(ELAPSED_ID);
  if (elapsedEl) {
    elapsedEl.textContent = `仍在准备中（已等待 ${Math.round(elapsed / 1000)} 秒）`;
    elapsedEl.style.display = '';
  }
  const actions = document.getElementById(ACTIONS_ID);
  if (actions) actions.style.display = '';
  bindSlowButtons();
}

function hideSlowActions(): void {
  document.getElementById(ELAPSED_ID)?.setAttribute('style', 'display:none');
  document.getElementById(ACTIONS_ID)?.setAttribute('style', 'display:none');
}

function bindSlowButtons(): void {
  if (buttonsBound) return;
  buttonsBound = true;
  document.getElementById(LOG_BTN_ID)?.addEventListener('click', () => {
    if (!currentLogPath) return;
    // Linux 打包态没有 desktop 可打开的启动日志——主进程改发 `hint:` 前缀的
    // systemctl/journalctl 命令串。弹层显示台账助排查，而非 openPath 打开一个空文件。
    if (currentLogPath.startsWith('hint:')) {
      showLogHint(currentLogPath.slice(5).trim());
      return;
    }
    void window.Crew?.openPath?.(currentLogPath);
  });
  document.getElementById(RETRY_BTN_ID)?.addEventListener('click', () => {
    // 重新拉起 gateway；重置计时，新一轮重新判定 slow。
    void window.Crew?.retryGateway?.();
    overlayShownAt = Date.now();
    slowDismissed = false;
    hideSlowActions();
  });
  document.getElementById(DISMISS_BTN_ID)?.addEventListener('click', () => {
    slowDismissed = true;
    hideSlowActions();
  });
}

/** 显示主进程下发的日志排查提示（Linux：systemctl/journalctl 命令）。 */
function showLogHint(text: string): void {
  let hint = document.getElementById('backend-loading-log-hint');
  if (!hint) {
    hint = document.createElement('div');
    hint.id = 'backend-loading-log-hint';
    hint.className = 'backend-loading-hint';
    const card = overlayEl?.querySelector('.backend-loading-card');
    card?.appendChild(hint);
  }
  // 提示是多行命令——按 •或换行分段显示，避免一长串挤成一团。
  hint.textContent = text;
  hint.style.display = '';
}

/**
 * gateway 晚于登录 hydrate 就绪时：补连 WS，并刷新模型配置。
 * 失败吞掉——下一次 backend:status / socket 自重连会再试。
 */
async function recoverAfterBackendConnected(): Promise<void> {
  if (recoverInFlight) return;
  recoverInFlight = true;
  try {
    const socket = uiStore.get().socket as BackendChatSocket | null;
    if (socket && typeof socket.connect === 'function' && !socket.isGatewayProxyOpen()) {
      socket.connect();
    }
    await Promise.allSettled([
      import('./model-picker').then((module) => module.loadConfig()),
    ]);
  } finally {
    recoverInFlight = false;
  }
}

/**
 * 初始化后端状态守卫：
 * 1. 订阅主进程 backend:status 推送
 * 2. 同步 uiStore.backendConnected
 * 3. 控制全局 Loading 遮罩显隐
 *
 * 幂等：多次调用安全，仅绑定一次监听器。
 */
export function initBackendStatusGuard(): void {
  if (initialized) return;
  initialized = true;

  // 初始态：后端尚未连接，立即展示遮罩（首帧就能看到）
  applyBackendOverlay(false);

  window.Crew?.onBackendStatus?.((status) => {
    const connected = !!status?.connected;
    const wasConnected = uiStore.get().backendConnected === true;
    if (status?.logPath) currentLogPath = status.logPath;
    uiStore.set({ backendConnected: connected });
    applyBackendOverlay(connected);
    const failedComponent = Object.values(status?.components ?? {})
      .find((component) => component.status === 'failed');
    const warning = connected && failedComponent
      ? (failedComponent.message || '运行环境组件初始化失败，请查看 Gateway 日志')
      : '';
    if (warning && warning !== lastComponentWarning) notify(warning);
    lastComponentWarning = warning;
    // 假阴性恢复：冷启动时登录 hydrate 打到未就绪 gateway，模型配置/WS 可能为空；
    // health 转正后补一次。
    if (connected && !wasConnected) {
      void recoverAfterBackendConnected();
    }
  });
}

/**
 * 关闭 init 阶段的旁路标记。由 init() 在所有 setTab/初始化完成后调用，
 * 此后 setTab 才会真正受后端状态守卫约束。
 */
export function sealBackendInitBypass(): void {
  initBypassActive = false;
}

/**
 * 当前是否允许 setTab 绕过守卫（仅 init 阶段为 true）。
 */
export function isBackendInitBypassActive(): boolean {
  return initBypassActive;
}

/**
 * 查询当前后端是否已连接（供 setTab 等路由守卫使用）。
 * 直接读 uiStore 而非 state shim，避免 Proxy 开销。
 */
export function isBackendConnected(): boolean {
  return uiStore.get().backendConnected === true;
}
