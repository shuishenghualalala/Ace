/**
 * 后端服务状态横幅 —— 页面顶部的非阻断状态提示。
 *
 * 监听主进程推送的 `backend:status` IPC 事件（周期健康检查 /api/health），
 * 后端不可用时在页面顶部展示状态横幅，恢复后自动消失。横幅不阻断任何
 * 交互：页面切换、历史会话和草稿在断连时依然可用；composer-view
 * 依据 uiStore.backendCanSend 控制发送。
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
import type { BackendStartupReadiness } from '../api/transport';
import { notify } from '../state';
import { uiStore } from '../stores/stores';

const BANNER_ID = 'backend-status-banner';
const TICK_MS = 1000;

type BackendFailureKind = 'unreachable' | 'timeout' | 'auth_failed' | 'unknown';
type TransportState = 'unknown' | 'connected' | 'reconnecting' | 'disconnected';

type BackendStatus = {
  connected: boolean;
  logPath?: string;
  components?: Record<string, { status: string; message?: string }>;
  failureKind?: BackendFailureKind;
  processState?: 'starting' | 'ready' | 'restarting' | 'offline' | 'error';
  healthState?: 'unknown' | 'healthy' | 'degraded' | 'stalled';
  transportState?: TransportState;
  readiness?: BackendStartupReadiness;
  loopLagMs?: number;
  /** 断开起始时间（epoch ms），仅 connected=false 时携带。 */
  since?: number;
};

type BannerState = 'starting' | 'connecting' | 'busy' | 'stalled' | 'restarting' | 'startup-failed' | 'error' | 'hidden';

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
let lastStatus: BackendStatus | undefined;
let socketState: TransportState = 'unknown';
let socketGeneration: number | undefined;
let lifecycle = 0;
let recoverAgain = false;
let recoveredGeneration: number | undefined;

function deriveState(status: BackendStatus, transport: TransportState): BannerState {
  if (status.failureKind === 'auth_failed') return 'error';
  if (status.readiness?.status === 'failed') return 'startup-failed';
  if (status.processState === 'error') return 'error';
  if (status.processState === 'restarting' || status.processState === 'offline') return 'restarting';
  if (status.healthState === 'stalled') return 'stalled';
  if (status.healthState === 'degraded') return 'busy';
  if (status.readiness?.status === 'starting' || (status.processState === 'starting' && !status.connected)) return 'starting';
  if (status.connected && status.readiness && transport !== 'connected') return 'connecting';
  if (status.connected) return 'hidden';
  switch (status.failureKind) {
    case 'timeout':
      return 'busy';
    case 'unreachable':
      return 'restarting';
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
  } else if (currentState === 'connecting') {
    el.classList.add('is-info');
    icon.textContent = '…';
    title.textContent = '正在连接聊天服务';
    text.textContent = '可先编辑草稿，连接后发送';
  } else if (currentState === 'startup-failed') {
    el.classList.add('is-danger');
    icon.textContent = '!';
    title.textContent = '后端启动失败';
    text.textContent = lastStatus?.components?.startup?.message || '聊天服务尚未可用，请查看日志或重试';
  } else if (currentState === 'busy') {
    el.classList.add('is-warn');
    icon.textContent = '!';
    title.textContent = '后端繁忙';
    const seconds = elapsedSeconds();
    const lag = lastStatus?.loopLagMs;
    text.textContent = lag !== undefined
      ? `业务循环延迟约 ${Math.round(lag)} ms，请稍候`
      : seconds == null ? '请求较多，请稍候' : `已等待 ${seconds} 秒，请稍候`;
  } else if (currentState === 'stalled') {
    el.classList.add('is-danger');
    icon.textContent = '!';
    title.textContent = '智能体服务卡顿';
    const lag = lastStatus?.loopLagMs;
    text.textContent = lag === undefined
      ? '业务循环暂时没有及时响应，可稍后重试'
      : `业务循环延迟约 ${Math.round(lag)} ms，正在观察恢复`;
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

function statusAllowsSending(status: BackendStatus, transport: TransportState): boolean {
  const processReady = !status.processState || status.processState === 'ready';
  const healthReady = !status.healthState
    || status.healthState === 'unknown'
    || status.healthState === 'healthy'
    || status.healthState === 'degraded';
  if (!status.connected || !processReady || !healthReady) return false;
  if (status.readiness) {
    return status.readiness.status === 'ready' && transport === 'connected';
  }
  // Older status senders do not necessarily expose transport.
  return transport === 'connected' || transport === 'unknown';
}

function connectionTransport(status: BackendStatus): TransportState {
  if (!status.readiness) return socketState === 'unknown' ? status.transportState ?? 'unknown' : socketState;
  if (socketGeneration !== status.readiness.generation) return 'unknown';
  if (socketState === 'connected') return status.transportState ?? 'unknown';
  return socketState;
}

function updateReadiness(status: BackendStatus): void {
  const transport = connectionTransport(status);
  const canSend = statusAllowsSending(status, transport);
  const wasReady = uiStore.get().backendCanSend;
  const connected = Boolean(status.connected);
  uiStore.set({
    backendConnected: connected,
    backendProcessState: status.processState ?? (connected ? 'ready' : 'starting'),
    backendHealthState: status.healthState ?? (connected ? 'healthy' : 'unknown'),
    backendReadiness: status.readiness ?? null,
    backendLoopLagMs: typeof status.loopLagMs === 'number' ? status.loopLagMs : null,
    backendTransportState: transport,
    backendCanSend: canSend,
  });
  setBannerState(deriveState(status, transport));
  if (canSend && !wasReady) void recoverAfterBackendConnected();
}

/** WS 只更新发送能力，不把 transport 断开误写成进程/HTTP 探针断开。 */
export function setBackendTransportState(
  transportState: TransportState,
  generation?: number,
): void {
  const current = lastStatus?.readiness?.generation;
  if (typeof generation === 'number'
    && (generation < (current ?? 0) || generation < (socketGeneration ?? 0))) return;
  if (current !== undefined && generation === undefined && transportState !== 'unknown') return;
  socketState = transportState;
  socketGeneration = generation;
  if (lastStatus) updateReadiness(lastStatus);
  else uiStore.set({ backendTransportState: transportState, backendCanSend: false });
}

/**
 * 在本窗口首次可以发送时拉取初始数据。普通 WS 重连由 session-controller
 * 对账，Gateway 代次切换时才重新拉取整套会话、工作区和配置。
 */
async function recoverAfterBackendConnected(): Promise<void> {
  const generation = lastStatus?.readiness?.generation;
  if (generation !== undefined && generation === recoveredGeneration) return;
  if (recoverInFlight) {
    recoverAgain = true;
    return;
  }
  recoverInFlight = true;
  const epoch = lifecycle;
  try {
    const { hydrateBackendState } = await import('./session-controller');
    if (epoch === lifecycle && canSendToBackend() && generation === lastStatus?.readiness?.generation) {
      await hydrateBackendState();
      if (epoch === lifecycle && generation === lastStatus?.readiness?.generation) {
        recoveredGeneration = generation;
      }
    }
  } catch {
    // The next connection-ready transition retries recovery.
  } finally {
    recoverInFlight = false;
    if (recoverAgain) {
      recoverAgain = false;
      if (canSendToBackend()) void recoverAfterBackendConnected();
    }
  }
}

/**
 * 初始化后端状态横幅：
 * 1. 立即展示「正在启动后端」（首帧就能看到）
 * 2. 订阅主进程 backend:status 推送，按状态机重渲横幅
 * 3. 分别同步运行状态和当前窗口的聊天发送能力
 *
 * 幂等：多次调用安全，仅绑定一次监听器。
 */
export function initBackendStatusGuard(): void {
  if (initialized) return;
  initialized = true;
  const epoch = ++lifecycle;

  setBannerState('starting');

  const applyStatus = (status: BackendStatus): void => {
    if (!status || epoch !== lifecycle) return;
    const previous = lastStatus;
    const readiness = status?.readiness;
    if (previous?.readiness && (!readiness || readiness.generation < previous.readiness.generation)) return;
    const connected = !!status?.connected;
    if (status?.logPath) currentLogPath = status.logPath;
    lastStatus = status;
    disconnectedSince = !connected && typeof status?.since === 'number' ? status.since : null;
    updateReadiness(status);
    const failedComponent = Object.entries(status?.components ?? {})
      .find(([name, component]) => component.status === 'failed' && (!readiness || name !== 'startup'))?.[1];
    const warning = connected && failedComponent
      ? (failedComponent.message || '运行环境组件初始化失败，请查看 Gateway 日志')
      : '';
    if (warning && warning !== lastComponentWarning) notify(warning);
    lastComponentWarning = warning;
    const socket = uiStore.get().socket as BackendChatSocket | null;
    if (readiness) socket?.setGatewayGeneration?.(readiness.generation);
    const coreBecameReady = readiness
      ? readiness.status === 'ready'
        && (previous?.readiness?.status !== 'ready' || readiness.generation !== previous.readiness.generation)
      : connected && !previous?.connected;
    if (coreBecameReady && typeof socket?.connect === 'function' && !socket.isGatewayProxyOpen?.()) {
      socket.connect();
    }
  };

  // reload 后 did-finish-load 可能早于 renderer 完成认证恢复，那次推送会丢失。
  // 先订阅后立即读一次主进程快照，保证已就绪时横幅不会永久停留。
  let pushed = false;
  unsubscribe = window.Crew?.onBackendStatus?.((status) => {
    pushed = true;
    applyStatus(status);
  }) ?? null;
  void window.Crew?.getBackendStatus?.().then((status) => {
    if (!pushed || (status.readiness && status.readiness.generation > (lastStatus?.readiness?.generation ?? -1))) {
      applyStatus(status);
    }
  }).catch(() => {
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
  lifecycle++;
  stopTick();
  document.getElementById(BANNER_ID)?.remove();
  bannerEl = null;
  currentState = 'hidden';
  disconnectedSince = null;
  currentLogPath = '';
  lastStatus = undefined;
  socketState = 'unknown';
  socketGeneration = undefined;
  recoverAgain = false;
  recoveredGeneration = undefined;
  lastComponentWarning = '';
  uiStore.set({ backendConnected: false, backendReadiness: null, backendTransportState: 'unknown', backendCanSend: false });
}

/**
 * 查询当前后端是否已连接（供 composer 等消费方使用）。
 * 直接读 uiStore 而非 state shim，避免 Proxy 开销。
 */
export function isBackendConnected(): boolean {
  return uiStore.get().backendConnected === true;
}

export function canSendToBackend(): boolean {
  return uiStore.get().backendCanSend === true;
}
