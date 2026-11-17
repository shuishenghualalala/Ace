import type { ChildProcess } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { GatewayProcessRetirement } from '../../src/main/gateway-process-retirement';

class TestChild extends EventEmitter {
  exitCode: number | null = null;
  signalCode: NodeJS.Signals | null = null;
  killed = false;

  constructor(readonly pid: number | undefined) {
    super();
  }

  readonly kill = vi.fn((_signal?: NodeJS.Signals): boolean => {
    this.killed = true;
    return true;
  });

  get process(): ChildProcess {
    return this as unknown as ChildProcess;
  }

  get dispatchedSignals(): NodeJS.Signals[] {
    return this.kill.mock.calls.map(([signal]) => signal ?? 'SIGTERM');
  }

  exit(code: number | null, signal: NodeJS.Signals | null = null): void {
    this.exitCode = code;
    this.signalCode = signal;
    this.emit('exit', code, signal);
  }
}

describe('GatewayProcessRetirement', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('starts with the owned lease and only forces the owned handle on timeout', async () => {
    const child = new TestChild(123);
    const peer = new TestChild(456);
    const requestStop = vi.fn();
    const retirement = new GatewayProcessRetirement({ graceMs: 20, killSettleMs: 5, requestStop });
    const stop = retirement.stop(child.process);

    expect(requestStop).toHaveBeenCalledExactlyOnceWith(child.process);
    expect(child.kill).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(20);
    expect(child.dispatchedSignals).toEqual(['SIGKILL']);
    expect(peer.kill).not.toHaveBeenCalled();
    child.exit(null, 'SIGKILL');
    await stop;
  });

  it('coalesces stops and waits for the graceful exit', async () => {
    const child = new TestChild(123);
    const retirement = new GatewayProcessRetirement({ graceMs: 20, killSettleMs: 5 });

    const stop = retirement.stop(child.process);
    expect(retirement.stop(child.process)).toBe(stop);
    expect(retirement.pending).toBe(stop);
    expect(retirement.child).toBe(child.process);
    expect(child.dispatchedSignals).toEqual(['SIGTERM']);
    expect(child.killed).toBe(true);

    await vi.advanceTimersByTimeAsync(10);
    expect(retirement.pending).toBe(stop);

    child.exit(0);
    await stop;
    expect(retirement.child).toBeNull();
    expect(retirement.pending).toBeNull();

    await vi.advanceTimersByTimeAsync(20);
    expect(child.dispatchedSignals).toEqual(['SIGTERM']);
  });

  it('escalates after the requested grace period and waits for an observed signal exit', async () => {
    const child = new TestChild(123);
    const retirement = new GatewayProcessRetirement({ graceMs: 30, killSettleMs: 5 });

    const stop = retirement.stop(child.process, 10);
    await vi.advanceTimersByTimeAsync(9);
    expect(child.dispatchedSignals).toEqual(['SIGTERM']);

    await vi.advanceTimersByTimeAsync(1);
    expect(child.dispatchedSignals).toEqual(['SIGTERM', 'SIGKILL']);
    expect(retirement.pending).toBe(stop);
    expect(retirement.child).toBe(child.process);

    child.exit(null, 'SIGKILL');
    await stop;
    expect(retirement.child).toBeNull();
    expect(retirement.pending).toBeNull();
  });

  it('retains ownership on the deadline and clears it when a late exit arrives', async () => {
    const child = new TestChild(123);
    const retirement = new GatewayProcessRetirement({ graceMs: 20, killSettleMs: 5 });

    const stop = retirement.stop(child.process);
    const timedOut = expect(stop).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(25);
    await timedOut;

    expect(child.dispatchedSignals).toEqual(['SIGTERM', 'SIGKILL']);
    expect(retirement.child).toBe(child.process);
    expect(retirement.pending).toBeNull();

    child.exit(null, 'SIGKILL');
    expect(retirement.child).toBeNull();
  });

  it('allows another stop attempt while a timed-out child is still owned', async () => {
    const child = new TestChild(123);
    const retirement = new GatewayProcessRetirement({ graceMs: 20, killSettleMs: 5 });
    const first = retirement.stop(child.process);
    const timedOut = expect(first).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(25);
    await timedOut;

    const retry = retirement.stop(child.process);
    expect(retry).not.toBe(first);
    expect(retirement.pending).toBe(retry);
    expect(retirement.child).toBe(child.process);
    expect(child.dispatchedSignals).toEqual(['SIGTERM', 'SIGKILL', 'SIGTERM']);

    child.exit(0);
    await retry;
    expect(retirement.child).toBeNull();
    expect(retirement.pending).toBeNull();
  });

  it('retains the child when signaling throws until actual termination is observed', async () => {
    const child = new TestChild(123);
    child.kill.mockImplementationOnce(() => {
      throw new Error('SIGTERM failed');
    });
    child.kill.mockImplementationOnce(() => {
      throw new Error('SIGKILL failed');
    });
    const retirement = new GatewayProcessRetirement({ graceMs: 20, killSettleMs: 5 });

    const stop = retirement.stop(child.process);
    const timedOut = expect(stop).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(25);
    await timedOut;

    expect(child.dispatchedSignals).toEqual(['SIGTERM', 'SIGKILL']);
    expect(retirement.child).toBe(child.process);
    child.exit(0);
    expect(retirement.child).toBeNull();
  });

  it('does not signal an exited child or a child without a spawned pid', async () => {
    const exited = new TestChild(123);
    exited.exit(0);
    const signaled = new TestChild(456);
    signaled.exit(null, 'SIGKILL');
    const notSpawned = new TestChild(undefined);
    const retirement = new GatewayProcessRetirement();

    await retirement.stop(exited.process);
    await retirement.stop(signaled.process);
    await retirement.stop(notSpawned.process);
    await retirement.stop(null);

    expect(exited.kill).not.toHaveBeenCalled();
    expect(signaled.kill).not.toHaveBeenCalled();
    expect(notSpawned.kill).not.toHaveBeenCalled();
    expect(retirement.child).toBeNull();
    expect(retirement.pending).toBeNull();
  });
});
