import type {
  GatewayComponentState,
  GatewayProbeFailureKind,
} from './gateway-instance-auth';

// 🌟 启动优化：健康探测间隔 1000ms，更快感知 gateway 就绪状态变化。
// 探针链式排程（上一次 probe 结束才排下一次），单次 probe 最长 3s 超时，
// 若用 setInterval 会在 gateway 繁忙时段自我叠加，拖垮主进程事件循环。
export const HEALTH_CHECK_INTERVAL_MS = 1_000;
// 稳定期：连续失败达到该次数才判 disconnected（gateway 单线程 asyncio 繁忙时
// 单次 3s health 超时属正常，立即判死会误弹遮罩）。
export const HEALTH_STABLE_FAIL_THRESHOLD = 3;
// 启动宽限期内阈值放宽：进程刚拉起时健康端点可能还没绑定端口 / 正在加载，
// 短窗口内容忍更多连续失败。
export const HEALTH_STARTUP_GRACE_MS = 30_000;
export const HEALTH_STARTUP_FAIL_THRESHOLD = 10;
export const HEALTH_DEGRADED_LAG_MS = 3_000;
export const HEALTH_STALLED_LAG_MS = 10_000;
export const HEALTH_LAG_TRANSITION_SAMPLES = 2;

export type BackendHealthState = 'unknown' | 'healthy' | 'degraded' | 'stalled';
export type BackendProcessState = 'starting' | 'ready' | 'restarting' | 'offline' | 'error';
export type BackendTransportState = 'unknown' | 'connected' | 'reconnecting' | 'disconnected';

export interface BackendHealthProbeResult {
  verified: boolean;
  components?: Record<string, GatewayComponentState>;
  failureKind?: GatewayProbeFailureKind;
  loopLagMs?: number;
}

/** 推送给 renderer 的 backend:status 变化负载（不含 baseUrl/logPath，由接线方补齐）。 */
export interface BackendHealthStatusChange {
  connected: boolean;
  components?: Record<string, GatewayComponentState>;
  /** 判 disconnected 时的最近失败类别；connected=true 时不带。 */
  failureKind?: GatewayProbeFailureKind;
  /** 本轮 outage 的首次失败时间戳（epoch ms）；connected=true 时不带。 */
  since?: number;
  healthState?: BackendHealthState;
  loopLagMs?: number;
}

export interface BackendHealthMonitorOptions {
  intervalMs?: number;
  stableFailThreshold?: number;
  startupGraceMs?: number;
  startupFailThreshold?: number;
  degradedLagMs?: number;
  stalledLagMs?: number;
  lagTransitionSamples?: number;
  now?: () => number;
}

/**
 * 串行化的 backend 健康探针循环。
 *
 * 用「链式 setTimeout」替代 setInterval：每次 probe 完成并间隔 intervalMs 后才
 * 发起下一次，probe 耗时超过间隔也绝不会叠加。双阈值：监控启动（或 markRestart）
 * 后 startupGraceMs 内用 startupFailThreshold，之后用 stableFailThreshold。
 * 一次成功立即恢复 connected 并清空计数。
 */
export class BackendHealthMonitor {
  private readonly intervalMs: number;
  private readonly stableFailThreshold: number;
  private readonly startupGraceMs: number;
  private readonly startupFailThreshold: number;
  private readonly degradedLagMs: number;
  private readonly stalledLagMs: number;
  private readonly lagTransitionSamples: number;
  private readonly now: () => number;

  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = true;
  private connected = false;
  private failCount = 0;
  private components: Record<string, GatewayComponentState> | undefined;
  private since: number | undefined;
  private startedAt = 0;
  private healthState: BackendHealthState = 'unknown';
  private lagTransitionCount = 0;

  constructor(
    private readonly probe: () => Promise<BackendHealthProbeResult>,
    private readonly push: (change: BackendHealthStatusChange) => void,
    options: BackendHealthMonitorOptions = {},
  ) {
    this.intervalMs = Math.max(1, options.intervalMs ?? HEALTH_CHECK_INTERVAL_MS);
    this.stableFailThreshold = Math.max(1, options.stableFailThreshold ?? HEALTH_STABLE_FAIL_THRESHOLD);
    this.startupGraceMs = Math.max(0, options.startupGraceMs ?? HEALTH_STARTUP_GRACE_MS);
    this.startupFailThreshold = Math.max(
      this.stableFailThreshold,
      options.startupFailThreshold ?? HEALTH_STARTUP_FAIL_THRESHOLD,
    );
    this.degradedLagMs = Math.max(1, options.degradedLagMs ?? HEALTH_DEGRADED_LAG_MS);
    this.stalledLagMs = Math.max(
      this.degradedLagMs,
      options.stalledLagMs ?? HEALTH_STALLED_LAG_MS,
    );
    this.lagTransitionSamples = Math.max(
      1,
      options.lagTransitionSamples ?? HEALTH_LAG_TRANSITION_SAMPLES,
    );
    this.now = options.now ?? Date.now;
  }

  /** 启动循环并立即发起第一次探测；重复调用是幂等的。 */
  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.startedAt = this.now();
    this.schedule(0);
  }

  /** 停止排程；在途 probe 的结果会被丢弃且不再排下一次。 */
  stop(): void {
    this.stopped = true;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  /** Gateway 重建（自动/手动重启）后调用：宽限期与失败计数复位。 */
  markRestart(): void {
    this.startedAt = this.now();
    this.failCount = 0;
    this.since = undefined;
    this.healthState = 'unknown';
    this.lagTransitionCount = 0;
  }

  isConnected(): boolean {
    return this.connected;
  }

  private schedule(delayMs: number): void {
    if (this.stopped) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.tick();
    }, delayMs);
  }

  private async tick(): Promise<void> {
    if (this.stopped) return;
    let result: BackendHealthProbeResult;
    try {
      result = await this.probe();
    } catch {
      result = { verified: false, failureKind: 'unknown' };
    }
    if (this.stopped) return;
    if (result.verified) this.onSuccess(result);
    else this.onFailure(result.failureKind ?? 'unknown');
    this.schedule(this.intervalMs);
  }

  private onSuccess(result: BackendHealthProbeResult): void {
    // HTTP/实例证明恢复要快；业务 loop 健康仍按连续样本恢复，避免单点抖动。
    this.failCount = 0;
    this.since = undefined;
    const componentsChanged = JSON.stringify(this.components) !== JSON.stringify(result.components);
    this.components = result.components;
    const nextHealthState = this.nextHealthState(result.loopLagMs);
    const healthChanged = nextHealthState !== this.healthState;
    this.healthState = nextHealthState;
    if (!this.connected || componentsChanged || healthChanged) {
      this.connected = true;
      this.push({
        connected: true,
        ...(result.components ? { components: result.components } : {}),
        healthState: this.healthState,
        ...(typeof result.loopLagMs === 'number' ? { loopLagMs: result.loopLagMs } : {}),
      });
    }
  }

  private nextHealthState(loopLagMs: number | undefined): BackendHealthState {
    const target = typeof loopLagMs !== 'number'
      ? 'healthy'
      : loopLagMs >= this.stalledLagMs
        ? 'stalled'
        : loopLagMs >= this.degradedLagMs
          ? 'degraded'
          : 'healthy';
    if (this.healthState === 'unknown') {
      if (target === 'healthy') {
        this.lagTransitionCount = 0;
        return target;
      }
      this.lagTransitionCount += 1;
      if (this.lagTransitionCount < this.lagTransitionSamples) return 'unknown';
      this.lagTransitionCount = 0;
      return target;
    }
    if (target === this.healthState) {
      this.lagTransitionCount = 0;
      return this.healthState;
    }
    this.lagTransitionCount += 1;
    if (this.lagTransitionCount < this.lagTransitionSamples) return this.healthState;
    this.lagTransitionCount = 0;
    return target;
  }

  private onFailure(failureKind: GatewayProbeFailureKind): void {
    this.failCount += 1;
    if (this.since === undefined) this.since = this.now();
    const inGrace = this.now() - this.startedAt < this.startupGraceMs;
    const threshold = inGrace ? this.startupFailThreshold : this.stableFailThreshold;
    // 只在 connected → disconnected 跃迁时推送一次；启动后从未连上时不推 disconnected，
    // 避免首屏「先 disconnected 再 connected」的闪跳。
    if (!this.connected || this.failCount < threshold) return;
    this.connected = false;
    this.components = undefined;
    this.push({
      connected: false,
      failureKind,
      ...(this.since !== undefined ? { since: this.since } : {}),
      healthState: 'unknown',
    });
  }
}
