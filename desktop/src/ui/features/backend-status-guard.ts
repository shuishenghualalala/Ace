/**
 * 后端服务状态横幅 —— 页面顶部的非阻断状态提示。
 *
 * 监听主进程推送的 `backend:status` IPC 事件（周期健康检查 /api/health），
 * 后端不可用时在页面顶部展示状态横幅，恢复后自动消失。横幅不阻断任何
 * 交互：页面切换、浏览历史会话等只读操作在断连时依然可用；聊天输入框
 * 的禁用由 composer-view 依据 uiStore.backendConnected 自行处理。
 *
 * 状态机由 connected + failureKind + since 派生：
 *   启动中（尚未收到失败分类）        「正在启动后端」
 *   timeout（探测超时）               「后端繁忙，已等待 Ns」（秒级刷新）
 *   unreachable（网关无响应）         「后端无响应，正在自动重启」
 *   auth_failed / unknown             「后端连接异常」
 *   connected=true                    隐藏
 *
 * 与 auth-gate（登录墙）正交：登录墙管身份，此横幅管后端服务可用性。
 */

import type { BackendChatSocket } from '../backend-client';
import { notify } from '../state';
import { uiStore } from '../stores/stores';

const BANNER_ID = 'backend-status-banner';
const TICK_MS = 1000;

type BackendFailureKind = 'unreachable' | 'timeout' | 'auth_failed' | 'unknown';

type BackendStatus = {
  connected: boolean;
  logPath?: string;
  components?: Record<string, { status: string; message?: string }>;
  failureKind?: BackendFailureKind;
  /** 断开起始时间（epoch ms），仅 connected=false 时携带。 */
  since?: number;
};

type BannerState = 'starting' | 'busy' | 'restarting' | 'error' | 'hidden';

let initialized = false;
let unsubscribe: (() => void) | null = null;
let bannerEl: HTMLElement | null = null;
/** 当前横幅状态机。 */
let currentState: BannerState = 'hidden';
/** timeout 分支的断开起始时间，驱动「已等待 Ns」秒级刷新。 */
let disconnectedSince: number | null = null;
let tickTimer: number | null = null;
let currentLogPath = '';
/** 避免 health 抖动时重复触发恢复 hydrate。 */
let recoverInFlight = false;
let lastComponentWarning = '';

function deriveState(status: BackendStatus | undefined): BannerState {
  if (!status || status.connected) return 'hidden';
  switch (status.failureKind) {
    case 'timeout':
      return 'busy';
    case 'unreachable':
      return 'restarting';
    case 'auth_failed':
    case 'unknown':
      return 'error';
    default:
      // 尚未收到失败分类：应用刚启动、首次连通前的过渡态。
      return 'starting';
  }
}

function mountContainer(): Element | null {
  return document.querySelector('.main-content');
}

function ensureBanner(): HTMLElement | null {
  if (bannerEl && bannerEl.isConnected) return bannerEl;
  const container = mountContainer();
  if (!container) return null;
  const el = document.createElement('div');
  el.id = BANNER_ID;
  el.className = 'backend-banner';
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
  el.innerHTML =
    '<span class="backend-banner__icon"></span>' +
    '<span class="backend-banner__content">' +
    '<strong class="backend-banner__title"></strong>' +
    '<span class="backend-banner__text"></span>' +
    '</span>' +
    '<span class="backend-banner__actions"></span>';
  container.insertBefore(el, container.firstChild);
  // 事件委托一次：data-action="log" 打开日志，data-action="retry" 重启 gateway。
  el.addEventListener('click', (event) => {
    const target = event.target as HTMLElement;
    if (target.dataset.action === 'log') openBackendLog();
    else if (target.dataset.action === 'retry') void window.Crew?.retryGateway?.();
  });
  bannerEl = el;
  return el;
}

function elapsedSeconds(): number | null {
  if (disconnectedSince == null) return null;
  return Math.max(0, Math.round((Date.now() - disconnectedSince) / 1000));
}

function applyState(el: HTMLElement): void {
  const visible = currentState !== 'hidden';
  el.classList.toggle('show', visible);
  if (!visible) return;
  el.classList.remove('is-info', 'is-warn', 'is-danger');
  const icon = el.querySelector('.backend-banner__icon') as HTMLElement;
  const title = el.querySelector('.backend-banner__title') as HTMLElement;
  const text = el.querySelector('.backend-banner__text') as HTMLElement;
  const actions = el.querySelector('.backend-banner__actions') as HTMLElement;
  if (currentState === 'starting') {
    el.classList.add('is-info');
    icon.textContent = '…';
    title.textContent = '正在启动后端';
    text.textContent = '智能体运行环境准备中，请稍等';
  } else if (currentState === 'busy') {
    el.classList.add('is-warn');
    icon.textContent = '!';
    title.textContent = '后端繁忙';
    const seconds = elapsedSeconds();
    text.textContent = seconds == null ? '请求较多，请稍候' : `已等待 ${seconds} 秒，请稍候`;
  } else if (currentState === 'restarting') {
    el.classList.add('is-warn');
    icon.textContent = '!';
    title.textContent = '后端无响应';
    text.textContent = '正在自动重启，请稍候';
  } else {
    el.classList.add('is-danger');
    icon.textContent = '!';
    title.textContent = '后端连接异常';
    text.textContent = '连接状态异常，请查看日志或重试';
  }
  actions.innerHTML =
    (currentLogPath
      ? '<button class="backend-banner__btn" data-action="log" type="button">查看日志</button>'
      : '') +
    '<button class="backend-banner__btn" data-action="retry" type="button">重试</button>';
}

/** 重渲横幅；挂载点尚不存在（DOM 未就绪）时跳过，等下一次状态推送再试。 */
function renderBanner(): void {
  const banner = ensureBanner();
  if (banner) applyState(banner);
}

function startTick(): void {
  if (tickTimer !== null) return;
  tickTimer = window.setInterval(() => {
    if (currentState === 'busy') renderBanner();
    else stopTick();
  }, TICK_MS);
}

function stopTick(): void {
  if (tickTimer !== null) {
    window.clearInterval(tickTimer);
    tickTimer = null;
  }
}

/** 打开主进程下发的 Gateway 日志；Linux 打包态下发的是 `hint:` 前缀的排查命令串，直接展示。 */
function openBackendLog(): void {
  if (!currentLogPath) return;
  if (currentLogPath.startsWith('hint:')) {
    const banner = bannerEl;
    const text = banner?.querySelector('.backend-banner__text');
    if (text) text.textContent = currentLogPath.slice(5).trim();
    return;
  }
  void window.Crew?.openPath?.(currentLogPath);
}

function setBannerState(next: BannerState): void {
  currentState = next;
  if (next === 'busy') startTick();
  else stopTick();
  renderBanner();
}

/**
 * gateway 晚于登录 hydrate 就绪时：补连 WS 并重拉配置。
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
    const recoveries: Promise<unknown>[] = [
      import('./model-picker').then((module) => module.loadConfig()),
    ];
    await Promise.allSettled(recoveries);
  } finally {
    recoverInFlight = false;
  }
}

/**
 * 初始化后端状态横幅：
 * 1. 立即展示「正在启动后端」（首帧就能看到）
 * 2. 订阅主进程 backend:status 推送，按状态机重渲横幅
 * 3. 同步 uiStore.backendConnected（composer 输入禁用等消费方依赖）
 *
 * 幂等：多次调用安全，仅绑定一次监听器。
 */
export function initBackendStatusGuard(): void {
  if (initialized) return;
  initialized = true;

  setBannerState('starting');

  const applyStatus = (status: BackendStatus): void => {
    const connected = !!status?.connected;
    const wasConnected = uiStore.get().backendConnected === true;
    if (status?.logPath) currentLogPath = status.logPath;
    disconnectedSince = !connected && typeof status?.since === 'number' ? status.since : null;
    uiStore.set({ backendConnected: connected });
    setBannerState(deriveState(status));
    const failedComponent = Object.values(status?.components ?? {})
      .find((component) => component.status === 'failed');
    const warning = connected && failedComponent
      ? (failedComponent.message || '运行环境组件初始化失败，请查看 Gateway 日志')
      : '';
    if (warning && warning !== lastComponentWarning) notify(warning);
    lastComponentWarning = warning;
    // 假阴性恢复：冷启动时登录 hydrate 打到未就绪 gateway，配置/WS 会空着；
    // health 转正后补一次，避免一直「服务未连接」。
    if (connected && !wasConnected) {
      void recoverAfterBackendConnected();
    }
  };

  // reload 后 did-finish-load 可能早于 renderer 完成认证恢复，那次推送会丢失。
  // 先订阅后立即读一次主进程快照，保证已就绪时横幅不会永久停留。
  unsubscribe = window.Crew?.onBackendStatus?.(applyStatus) ?? null;
  void window.Crew?.getBackendStatus?.().then(applyStatus).catch(() => {
    // 保持启动中横幅，后续健康状态推送会继续接管。
  });
}

/**
 * 反初始化：解绑订阅、移除横幅 DOM、停掉计时器。
 * 供测试与 renderer 热卸载使用；dispose 后可重新 init。
 */
export function disposeBackendStatusGuard(): void {
  unsubscribe?.();
  unsubscribe = null;
  initialized = false;
  stopTick();
  document.getElementById(BANNER_ID)?.remove();
  bannerEl = null;
  currentState = 'hidden';
  disconnectedSince = null;
  currentLogPath = '';
  lastComponentWarning = '';
}

/**
 * 查询当前后端是否已连接（供 composer 等消费方使用）。
 * 直接读 uiStore 而非 state shim，避免 Proxy 开销。
 */
export function isBackendConnected(): boolean {
  return uiStore.get().backendConnected === true;
}
