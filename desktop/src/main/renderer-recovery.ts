export interface RendererFailure {
  reason: 'crashed' | 'abnormal-exit' | 'oom' | 'launch-failed' | 'integrity-failure'
    | 'killed' | 'clean-exit' | 'memory-eviction' | 'load-failed' | 'startup-timeout' | 'unresponsive';
  code?: number;
  signal?: 'heartbeat';
}

export interface RendererWatchState {
  visible: boolean;
  minimized: boolean;
  focused: boolean;
}

interface RecoveryEvent {
  event: 'renderer-started' | 'renderer-ready' | 'renderer-failed' | 'renderer-retry'
    | 'renderer-manual-retry' | 'renderer-exhausted' | 'renderer-unresponsive' | 'renderer-responsive'
    | 'renderer-heartbeat-probe-sent' | 'renderer-heartbeat-probe-responded'
    | 'renderer-heartbeat-monitor-reset';
  documentId: number;
  attempts: number;
  failure?: RendererFailure;
  elapsedMs?: number;
  heartbeatAgeMs?: number;
  probeElapsedMs?: number;
  monitorDelayMs?: number;
  monitorResetReason?: 'main-process-pause' | 'window-not-monitored' | 'window-monitored';
  windowState?: RendererWatchState;
}

type RecoveryEventDetails = Partial<Omit<RecoveryEvent, 'event' | 'documentId' | 'attempts'>>;

const recoveryTimings = {
  startupMs: 20_000,
  hangMs: 12_000,
  heartbeatMs: 16_000,
  stableMs: 60_000,
  retryDelaysMs: [200, 750, 1_500] as readonly number[],
};

/** Tracks failures across replacement windows. Only a sustained healthy document resets the budget. */
export class RendererRecovery {
  private documentId = 0;
  private phase: 'closed' | 'starting' | 'ready' | 'waiting' | 'exhausted' = 'closed';
  private attempts = 0;
  private phaseTimer: ReturnType<typeof setTimeout> | undefined;
  private hangTimer: ReturnType<typeof setTimeout> | undefined;
  private heartbeatTimer: ReturnType<typeof setInterval> | undefined;
  private heartbeatAt = 0;
  private heartbeatProbeAt: number | undefined;
  private startedAt = 0;
  private hungAt = 0;

  constructor(
    private readonly callbacks: {
      record(event: RecoveryEvent): void;
      release(documentId: number): void;
      restart(documentId: number): void;
      exhausted(documentId: number, failure: RendererFailure): void;
      watchHeartbeat?(documentId: number): RendererWatchState | null;
      probeHeartbeat?(documentId: number): void;
    },
    private readonly timings = recoveryTimings,
  ) {}

  begin(): number {
    this.clearTimers();
    const id = ++this.documentId;
    this.phase = 'starting';
    this.startedAt = performance.now();
    this.emit('renderer-started');
    this.phaseTimer = setTimeout(() => this.fail(id, { reason: 'startup-timeout' }), this.timings.startupMs);
    return id;
  }

  isReady(id: number): boolean {
    return this.documentId === id && this.phase === 'ready';
  }

  canServe(id: number): boolean {
    return this.documentId === id && (this.phase === 'starting' || this.phase === 'ready');
  }

  ready(id: number): void {
    if (this.documentId !== id || this.phase !== 'starting') return;
    this.phase = 'ready';
    clearTimeout(this.phaseTimer);
    this.emit('renderer-ready', { elapsedMs: Math.round(performance.now() - this.startedAt) });
    this.armStableReset(id);
    this.armHeartbeatWatch(id);
  }

  heartbeat(id: number, source: 'interval' | 'probe' = 'interval'): void {
    if (!this.isReady(id)) return;
    const now = performance.now();
    if (source === 'probe' && this.heartbeatProbeAt !== undefined) {
      this.emit('renderer-heartbeat-probe-responded', {
        heartbeatAgeMs: Math.round(now - this.heartbeatAt),
        probeElapsedMs: Math.round(now - this.heartbeatProbeAt),
      });
    }
    this.heartbeatProbeAt = undefined;
    this.heartbeatAt = now;
  }

  unresponsive(id: number): void {
    if (!this.canServe(id) || this.hangTimer !== undefined) return;
    this.hungAt = performance.now();
    if (this.isReady(id)) clearTimeout(this.phaseTimer);
    this.emit('renderer-unresponsive');
    this.hangTimer = setTimeout(() => this.fail(id, { reason: 'unresponsive' }), this.timings.hangMs);
  }

  responsive(id: number): void {
    if (!this.canServe(id) || this.hangTimer === undefined) return;
    clearTimeout(this.hangTimer);
    this.hangTimer = undefined;
    this.emit('renderer-responsive', { elapsedMs: Math.round(performance.now() - this.hungAt) });
    if (this.isReady(id)) this.armStableReset(id);
  }

  fail(id: number, failure: RendererFailure, details: RecoveryEventDetails = {}): void {
    if (!this.canServe(id)) return;
    this.phase = 'waiting';
    this.clearTimers();
    this.emit('renderer-failed', { ...details, failure });
    try {
      this.callbacks.release(id);
    } finally {
      this.scheduleRestart(id, failure);
    }
  }

  replacementFailed(id: number): void {
    if (this.documentId !== id || this.phase !== 'waiting') return;
    this.emit('renderer-failed', { failure: { reason: 'launch-failed' } });
    this.scheduleRestart(id, { reason: 'launch-failed' });
  }

  retry(id: number): void {
    if (this.documentId !== id || this.phase !== 'exhausted') return;
    this.attempts = 0;
    this.phase = 'waiting';
    this.emit('renderer-manual-retry');
    this.callbacks.restart(id);
  }

  close(id: number): void {
    if (this.documentId !== id) return;
    this.clearTimers();
    this.attempts = 0;
    this.phase = 'closed';
  }

  private armStableReset(id: number): void {
    if (this.hangTimer !== undefined) return;
    this.phaseTimer = setTimeout(() => {
      if (this.isReady(id)) this.attempts = 0;
    }, this.timings.stableMs);
  }

  private armHeartbeatWatch(id: number): void {
    const watch = this.callbacks.watchHeartbeat;
    if (!watch) return;
    const intervalMs = this.timings.heartbeatMs / 4;
    let lastCheckAt = performance.now();
    let probePending = false;
    let wasForeground = true;
    this.heartbeatAt = lastCheckAt;
    this.heartbeatTimer = setInterval(() => {
      if (!this.isReady(id)) return;
      const now = performance.now();
      const monitorDelayMs = now - lastCheckAt;
      const mainWasPaused = monitorDelayMs > intervalMs * 2;
      lastCheckAt = now;
      const windowState = watch(id);
      const isForeground = Boolean(
        windowState?.visible && !windowState.minimized && windowState.focused,
      );
      // A hidden, minimized, or unfocused window can be intentionally throttled.
      // After sleep or a main-process stall, give both processes time to resume.
      if (mainWasPaused) {
        this.emit('renderer-heartbeat-monitor-reset', {
          monitorResetReason: 'main-process-pause',
          monitorDelayMs: Math.round(monitorDelayMs),
          ...(windowState ? { windowState } : {}),
        });
        wasForeground = isForeground;
        this.heartbeatAt = now;
        probePending = false;
        this.heartbeatProbeAt = undefined;
        return;
      }
      if (!isForeground) {
        if (wasForeground) {
          this.emit('renderer-heartbeat-monitor-reset', {
            monitorResetReason: 'window-not-monitored',
            monitorDelayMs: Math.round(monitorDelayMs),
            ...(windowState ? { windowState } : {}),
          });
        }
        wasForeground = false;
        this.heartbeatAt = now;
        probePending = false;
        this.heartbeatProbeAt = undefined;
        return;
      }
      if (!wasForeground) {
        this.emit('renderer-heartbeat-monitor-reset', {
          monitorResetReason: 'window-monitored',
          monitorDelayMs: Math.round(monitorDelayMs),
          ...(windowState ? { windowState } : {}),
        });
      }
      wasForeground = true;
      if (now - this.heartbeatAt < this.timings.heartbeatMs) {
        probePending = false;
        return;
      }
      if (!probePending && this.callbacks.probeHeartbeat) {
        probePending = true;
        this.heartbeatProbeAt = now;
        this.emit('renderer-heartbeat-probe-sent', {
          heartbeatAgeMs: Math.round(now - this.heartbeatAt),
          monitorDelayMs: Math.round(monitorDelayMs),
          ...(windowState ? { windowState } : {}),
        });
        this.callbacks.probeHeartbeat(id);
        return;
      }
      this.fail(id, { reason: 'unresponsive', signal: 'heartbeat' }, {
        heartbeatAgeMs: Math.round(now - this.heartbeatAt),
        ...(this.heartbeatProbeAt !== undefined
          ? { probeElapsedMs: Math.round(now - this.heartbeatProbeAt) }
          : {}),
        monitorDelayMs: Math.round(monitorDelayMs),
        ...(windowState ? { windowState } : {}),
      });
    }, intervalMs);
  }

  private scheduleRestart(id: number, failure: RendererFailure): void {
    const delay = this.timings.retryDelaysMs[this.attempts];
    if (delay === undefined || failure.reason === 'integrity-failure') {
      this.phase = 'exhausted';
      this.emit('renderer-exhausted', { failure });
      this.callbacks.exhausted(id, failure);
      return;
    }
    this.attempts++;
    this.phaseTimer = setTimeout(() => {
      if (this.documentId !== id || this.phase !== 'waiting') return;
      this.emit('renderer-retry', { failure });
      this.callbacks.restart(id);
    }, delay);
  }

  private emit(event: RecoveryEvent['event'], details: RecoveryEventDetails = {}): void {
    this.callbacks.record({ event, documentId: this.documentId, attempts: this.attempts, ...details });
  }

  private clearTimers(): void {
    clearTimeout(this.phaseTimer);
    clearTimeout(this.hangTimer);
    clearInterval(this.heartbeatTimer);
    this.phaseTimer = undefined;
    this.hangTimer = undefined;
    this.heartbeatTimer = undefined;
    this.heartbeatProbeAt = undefined;
  }
}

export type { RecoveryEvent };
