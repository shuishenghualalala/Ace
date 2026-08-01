export interface GatewayRestartOptions {
  minDelayMs?: number;
  maxDelayMs?: number;
}

/** Serializes automatic Gateway restarts and backs off after failed attempts. */
export class GatewayRestartController {
  private timer: ReturnType<typeof setTimeout> | null = null;
  private running = false;
  private pending = false;
  private stopped = false;
  private failures = 0;
  private readonly minDelayMs: number;
  private readonly maxDelayMs: number;

  constructor(
    private readonly restart: () => Promise<void>,
    options: GatewayRestartOptions = {},
  ) {
    this.minDelayMs = Math.max(0, options.minDelayMs ?? 500);
    this.maxDelayMs = Math.max(this.minDelayMs, options.maxDelayMs ?? 30_000);
  }

  /** Coalesce exit notifications into one restart attempt. */
  schedule(): void {
    if (this.stopped) return;
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

  stop(): void {
    this.stopped = true;
    this.pending = false;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  private async run(): Promise<void> {
    if (this.stopped || this.running) return;
    this.running = true;
    this.pending = false;
    try {
      await this.restart();
      this.failures = 0;
    } catch {
      this.failures += 1;
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
