import type { ChildProcess } from 'node:child_process';

interface GatewayProcessRetirementOptions {
  graceMs?: number;
  killSettleMs?: number;
}

export class GatewayStopTimeoutError extends Error {
  constructor(pid: number | undefined) {
    super(`Gateway process ${pid ?? 'unknown'} did not exit after termination`);
    this.name = 'GatewayStopTimeoutError';
  }
}

function hasExited(child: ChildProcess): boolean {
  return child.exitCode !== null || child.signalCode !== null || child.pid === undefined;
}

/**
 * Holds the retiring process until an exit is observed. Callers share the
 * current stop attempt and may retry it if a live process outlasts its deadline.
 */
export class GatewayProcessRetirement {
  private retiringChild: ChildProcess | null = null;
  private stopAttempt: Promise<void> | null = null;
  private readonly graceMs: number;
  private readonly killSettleMs: number;

  constructor(options: GatewayProcessRetirementOptions = {}) {
    this.graceMs = options.graceMs ?? 3_000;
    this.killSettleMs = options.killSettleMs ?? 400;
  }

  get child(): ChildProcess | null {
    return this.retiringChild;
  }

  get pending(): Promise<void> | null {
    return this.stopAttempt;
  }

  stop(child: ChildProcess | null, graceMs = this.graceMs): Promise<void> {
    if (this.stopAttempt) return this.stopAttempt;
    const target = this.retiringChild ?? child;
    if (!target || hasExited(target)) {
      this.retiringChild = null;
      return Promise.resolve();
    }

    if (this.retiringChild !== target) {
      this.retiringChild = target;
      target.once('exit', () => {
        if (this.retiringChild === target) this.retiringChild = null;
      });
    }

    this.stopAttempt = this.waitForExit(target, graceMs).finally(() => {
      if (hasExited(target) && this.retiringChild === target) this.retiringChild = null;
      this.stopAttempt = null;
    });
    return this.stopAttempt;
  }

  private waitForExit(child: ChildProcess, graceMs: number): Promise<void> {
    return new Promise((resolve, reject) => {
      let timer: ReturnType<typeof setTimeout>;
      const onExit = () => {
        clearTimeout(timer);
        resolve();
      };
      const signal = (name: NodeJS.Signals) => {
        try {
          child.kill(name);
        } catch {
          // A failed signal does not prove that the child stopped.
        }
      };

      child.once('exit', onExit);
      timer = setTimeout(() => {
        timer = setTimeout(() => {
          child.off('exit', onExit);
          if (hasExited(child)) resolve();
          else reject(new GatewayStopTimeoutError(child.pid));
        }, this.killSettleMs);
        signal('SIGKILL');
      }, graceMs);
      signal('SIGTERM');
    });
  }
}
