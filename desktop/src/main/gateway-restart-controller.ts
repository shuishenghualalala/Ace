export interface GatewayRestartOptions {
  minDelayMs?: number;
  maxDelayMs?: number;
  /** 子进程从「健康就绪」存活超过该时长视为长命实例（清零熔断计数）。 */
  stableUptimeMs?: number;
  /** 连续短命/失败重启达到该次数后熔断：停止自动重启，等用户手动重试。 */
  maxConsecutiveFailures?: number;
  /** 判定「不计数」的异常（如用户重试作废旧流程的 GatewaySupersededError）。 */
  isIgnorableError?: (error: unknown) => boolean;
  /** 熔断触发时的回调（置终态横幅、记录 supervisor 决策）。 */
  onTripped?: (info: { consecutiveFailures: number }) => void;
}

/**
 * Serializes automatic Gateway restarts and backs off after failed attempts.
 *
 * 恢复梯子（三段）：
 * 1. 自动重启 + 指数退避（minDelay ×2ⁿ 封顶 maxDelay）；
 * 2. 短命实例计数（就绪后存活 < stableUptimeMs 即失败）与抛错重启共用退避指数；
 * 3. 连续失败达到 maxConsecutiveFailures 熔断：schedule() 不再自动拉起，
 *    由用户经横幅「重试」调用 reset() 后恢复。
 */
export class GatewayRestartController {
  private timer: ReturnType<typeof setTimeout> | null = null;
  private running = false;
  private pending = false;
  private stopped = false;
  private failures = 0;
  /** 连续短命实例数（熔断计数；restart() 成功不清零，只有长命实例/复位清零）。 */
  private consecutiveFailures = 0;
  private tripped = false;
  /** 最近一次实例健康就绪时间戳；null = 从未就绪（启动期崩溃由抛错路径计数）。 */
  private readyAt: number | null = null;
  /** 主动回收（用户重试/卡死回收/身份失效）预期内的退出不计失败。 */
  private expectIntentionalExit = false;
  private readonly minDelayMs: number;
  private readonly maxDelayMs: number;
  private readonly stableUptimeMs: number;
  private readonly maxConsecutiveFailures: number;

  constructor(
    private readonly restart: () => Promise<void>,
    options: GatewayRestartOptions = {},
  ) {
    this.minDelayMs = Math.max(0, options.minDelayMs ?? 500);
    this.maxDelayMs = Math.max(this.minDelayMs, options.maxDelayMs ?? 30_000);
    this.stableUptimeMs = Math.max(1, options.stableUptimeMs ?? 60_000);
    this.maxConsecutiveFailures = Math.max(1, options.maxConsecutiveFailures ?? 5);
    this.isIgnorableError = options.isIgnorableError ?? (() => false);
    if (options.onTripped) this.onTripped = options.onTripped;
  }

  private readonly isIgnorableError: (error: unknown) => boolean;
  private readonly onTripped?: (info: { consecutiveFailures: number }) => void;

  /** Coalesce exit notifications into one restart attempt. 熔断后不再自动拉起。 */
  schedule(): void {
    if (this.stopped || this.tripped) return;
    if (this.running) {
      this.pending = true;
      return;
    }
    if (this.timer) return;
    const delay = Math.min(this.maxDelayMs, this.minDelayMs * (2 ** this.failures));
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.run();
    }, delay);
  }

  /** 熔断后的手动复位（用户点「重试」）：清零计数并允许后续自动重启。 */
  reset(): void {
    this.tripped = false;
    this.consecutiveFailures = 0;
    this.failures = 0;
    this.readyAt = null;
    this.expectIntentionalExit = false;
  }

  isTripped(): boolean {
    return this.tripped;
  }

  stop(): void {
    this.stopped = true;
    this.pending = false;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  /** 实例健康就绪（ensureGateway 成功且为托管实例）时记录起点。 */
  noteGatewayReady(): void {
    this.readyAt = Date.now();
  }

  /** 主动回收前调用：下一次 exit 不计失败（用户重试 / 卡死回收 / 身份失效重建）。 */
  noteIntentionalStop(): void {
    this.expectIntentionalExit = true;
  }

  /** 子进程退出时调用：短命实例（就绪后存活 < stableUptimeMs）计入熔断。 */
  noteGatewayExit(): void {
    if (this.expectIntentionalExit) {
      this.expectIntentionalExit = false;
      this.readyAt = null;
      return;
    }
    if (this.readyAt === null) {
      // 从未健康就绪（启动期崩溃）：该路径由 ensureGateway 抛错 → run() catch 计数。
      return;
    }
    const aliveMs = Date.now() - this.readyAt;
    this.readyAt = null;
    if (aliveMs >= this.stableUptimeMs) {
      this.consecutiveFailures = 0;
      return;
    }
    this.consecutiveFailures += 1;
    this.failures += 1;
    if (!this.tripped && this.consecutiveFailures >= this.maxConsecutiveFailures) {
      this.tripped = true;
      try {
        this.onTripped?.({ consecutiveFailures: this.consecutiveFailures });
      } catch {
        /* 回调失败不影响熔断状态 */
      }
    }
  }

  private async run(): Promise<void> {
    if (this.stopped || this.running) return;
    this.running = true;
    this.pending = false;
    try {
      await this.restart();
      this.failures = 0;
    } catch (error) {
      if (this.isIgnorableError?.(error)) {
        // 被更新代际作废（用户重试让位）：不计失败、不退避、不重排。
        this.running = false;
        this.pending = false;
        return;
      }
      this.failures += 1;
      this.consecutiveFailures += 1;
      if (!this.tripped && this.consecutiveFailures >= this.maxConsecutiveFailures) {
        this.tripped = true;
        try {
          this.onTripped?.({ consecutiveFailures: this.consecutiveFailures });
        } catch {
          /* 回调失败不影响熔断状态 */
        }
        this.running = false;
        this.pending = false;
        return;
      }
      this.running = false;
      this.pending = false;
      this.schedule();
      return;
    }
    this.running = false;
    if (this.pending) {
      this.pending = false;
      this.schedule();
    }
  }
}
