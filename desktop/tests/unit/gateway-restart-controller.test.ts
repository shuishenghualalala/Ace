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
});
