export interface RendererFailure {
  reason: 'crashed' | 'abnormal-exit' | 'oom' | 'launch-failed' | 'integrity-failure'
    | 'killed' | 'clean-exit' | 'memory-eviction' | 'load-failed' | 'startup-timeout' | 'unresponsive';
  code?: number;
  signal?: 'heartbeat';
}

interface RecoveryEvent {
  event: 'renderer-started' | 'renderer-ready' | 'renderer-failed' | 'renderer-retry'
    | 'renderer-manual-retry' | 'renderer-exhausted' | 'renderer-unresponsive' | 'renderer-responsive';
  documentId: number;
  attempts: number;
  failure?: RendererFailure;
  elapsedMs?: number;
}

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
  private startedAt = 0;
  private hungAt = 0;

  constructor(
    private readonly callbacks: {
      record(event: RecoveryEvent): void;
      release(documentId: number): void;
      restart(documentId: number): void;
      exhausted(documentId: number, failure: RendererFailure): void;
      watchHeartbeat?(documentId: number): boolean;
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

  heartbeat(id: number): void {
    if (this.isReady(id)) this.heartbeatAt = performance.now();
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

  fail(id: number, failure: RendererFailure): void {
    if (!this.canServe(id)) return;
    this.phase = 'waiting';
    this.clearTimers();
    this.emit('renderer-failed', { failure });
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
    this.heartbeatAt = lastCheckAt;
    this.heartbeatTimer = setInterval(() => {
      if (!this.isReady(id)) return;
      const now = performance.now();
      const mainWasPaused = now - lastCheckAt > intervalMs * 2;
      lastCheckAt = now;
      // An occluded or minimized window can be intentionally throttled.
      // After sleep or a main-process stall, give both processes time to resume.
      if (mainWasPaused || !watch(id)) {
        this.heartbeatAt = now;
        probePending = false;
        return;
      }
      if (now - this.heartbeatAt < this.timings.heartbeatMs) {
        probePending = false;
        return;
      }
      if (!probePending && this.callbacks.probeHeartbeat) {
        probePending = true;
        this.callbacks.probeHeartbeat(id);
        return;
      }
      this.fail(id, { reason: 'unresponsive', signal: 'heartbeat' });
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

  private emit(event: RecoveryEvent['event'], details: Pick<RecoveryEvent, 'failure' | 'elapsedMs'> = {}): void {
    this.callbacks.record({ event, documentId: this.documentId, attempts: this.attempts, ...details });
  }

  private clearTimers(): void {
    clearTimeout(this.phaseTimer);
    clearTimeout(this.hangTimer);
    clearInterval(this.heartbeatTimer);
    this.phaseTimer = undefined;
    this.hangTimer = undefined;
    this.heartbeatTimer = undefined;
  }
}

export type { RecoveryEvent };
