import { describe, expect, it, vi } from 'vitest';

import { GatewayRestartController } from '../../src/main/gateway-restart-controller';

describe('GatewayRestartController', () => {
  it('coalesces duplicate exit signals and retries one restart at a time', async () => {
    vi.useFakeTimers();
    let running = 0;
    let maxRunning = 0;
    const restart = vi.fn(async () => {
      running += 1;
      maxRunning = Math.max(maxRunning, running);
      await Promise.resolve();
      running -= 1;
      if (restart.mock.calls.length === 1) throw new Error('first restart failed');
    });
    const controller = new GatewayRestartController(restart, {
      minDelayMs: 100,
      maxDelayMs: 1_000,
    });

    controller.schedule();
    controller.schedule();
    await vi.advanceTimersByTimeAsync(100);
    expect(restart).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(200);
    expect(restart).toHaveBeenCalledTimes(2);
    expect(maxRunning).toBe(1);
    controller.stop();
    vi.useRealTimers();
  });

  it('cancels a pending restart during application shutdown', async () => {
    vi.useFakeTimers();
    const restart = vi.fn(async () => undefined);
    const controller = new GatewayRestartController(restart, {
      minDelayMs: 100,
      maxDelayMs: 100,
    });

    controller.schedule();
    controller.stop();
    await vi.advanceTimersByTimeAsync(100);

    expect(restart).not.toHaveBeenCalled();
    vi.useRealTimers();
  });

  it('queues another attempt when the replacement exits during restart', async () => {
    vi.useFakeTimers();
    const restart = vi.fn(async () => undefined);
    const controller = new GatewayRestartController(restart, {
      minDelayMs: 100,
      maxDelayMs: 100,
    });
    restart.mockImplementation(async () => {
      if (restart.mock.calls.length === 1) controller.schedule();
    });

    controller.schedule();
    await vi.advanceTimersByTimeAsync(100);
    await vi.advanceTimersByTimeAsync(100);

    expect(restart).toHaveBeenCalledTimes(2);
    controller.stop();
    vi.useRealTimers();
  });

  it('trips the circuit breaker after consecutive short-lived instances', async () => {
    vi.useFakeTimers();
    const tripped = vi.fn();
    const restart = vi.fn(async () => undefined);
    const controller = new GatewayRestartController(restart, {
      minDelayMs: 100,
      maxDelayMs: 100,
      stableUptimeMs: 60_000,
      maxConsecutiveFailures: 2,
      onTripped: tripped,
    });

    // 两次短命实例（就绪后 <60s 退出）→ 熔断。
    // 第一次退出调度的重启定时器会在循环的时间推进中合法触发一次 restart()。
    for (let i = 0; i < 2; i++) {
      controller.noteGatewayReady();
      await vi.advanceTimersByTimeAsync(1_000);
      controller.noteGatewayExit();
      controller.schedule();
    }
    await vi.advanceTimersByTimeAsync(1_000);
    expect(tripped).toHaveBeenCalledTimes(1);
    expect(controller.isTripped()).toBe(true);

    // 熔断后 schedule() 不再自动拉起：重启次数不再增长。
    const callsAfterTrip = restart.mock.calls.length;
    controller.schedule();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(restart.mock.calls.length).toBe(callsAfterTrip);

    // 用户手动重试复位：恢复自动重启资格。
    controller.reset();
    expect(controller.isTripped()).toBe(false);
    controller.schedule();
    await vi.advanceTimersByTimeAsync(100);
    expect(restart.mock.calls.length).toBe(callsAfterTrip + 1);
    controller.stop();
    vi.useRealTimers();
  });

  it('long-lived instance exit resets the consecutive failure count', async () => {
    vi.useFakeTimers();
    const tripped = vi.fn();
    const controller = new GatewayRestartController(async () => undefined, {
      stableUptimeMs: 60_000,
      maxConsecutiveFailures: 2,
      onTripped: tripped,
    });

    controller.noteGatewayReady();
    await vi.advanceTimersByTimeAsync(1_000);
    controller.noteGatewayExit(); // 第一次短命 → 计数 1
    controller.noteGatewayReady();
    await vi.advanceTimersByTimeAsync(120_000);
    controller.noteGatewayExit(); // 长命 → 清零
    controller.noteGatewayReady();
    await vi.advanceTimersByTimeAsync(1_000);
    controller.noteGatewayExit(); // 又一次短命 → 计数 1，不熔断

    expect(tripped).not.toHaveBeenCalled();
    expect(controller.isTripped()).toBe(false);
    vi.useRealTimers();
  });

  it('intentional stop (user retry / stalled recycle) is not counted as failure', async () => {
    vi.useFakeTimers();
    const tripped = vi.fn();
    const controller = new GatewayRestartController(async () => undefined, {
      stableUptimeMs: 60_000,
      maxConsecutiveFailures: 2,
      onTripped: tripped,
    });

    for (let i = 0; i < 5; i++) {
      controller.noteGatewayReady();
      await vi.advanceTimersByTimeAsync(1_000);
      controller.noteIntentionalStop();
      controller.noteGatewayExit();
    }
    expect(tripped).not.toHaveBeenCalled();
    expect(controller.isTripped()).toBe(false);
    vi.useRealTimers();
  });

  it('ignorable errors (superseded waits) do not inflate backoff or count failures', async () => {
    vi.useFakeTimers();
    class Superseded extends Error {}
    const restart = vi.fn(async () => {
      throw new Superseded('gateway wait superseded by retry');
    });
    const controller = new GatewayRestartController(restart, {
      minDelayMs: 100,
      maxDelayMs: 100,
      isIgnorableError: (error) => error instanceof Superseded,
    });

    controller.schedule();
    await vi.advanceTimersByTimeAsync(100);
    expect(restart).toHaveBeenCalledTimes(1);
    // 可忽略错误：不重排、不计数——再等 1s 也不应有第二次自动重启。
    await vi.advanceTimersByTimeAsync(1_000);
    expect(restart).toHaveBeenCalledTimes(1);
    controller.stop();
    vi.useRealTimers();
  });
});
