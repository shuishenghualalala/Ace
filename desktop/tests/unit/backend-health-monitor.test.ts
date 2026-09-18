import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  BackendHealthMonitor,
  type BackendHealthProbeResult,
  type BackendHealthStatusChange,
} from '../../src/main/backend-health-monitor';

const OK: BackendHealthProbeResult = {
  verified: true,
  components: { cron: { status: 'ok' } },
};

function fail(kind: BackendHealthProbeResult['failureKind']): BackendHealthProbeResult {
  return { verified: false, failureKind: kind };
}

/** 可手动切换结果的 probe；按调用顺序返回预设队列。 */
function scriptedProbe(script: BackendHealthProbeResult[]) {
  const calls: number[] = [];
  let running = 0;
  let maxRunning = 0;
  const probe = vi.fn(async () => {
    calls.push(Date.now());
    running += 1;
    maxRunning = Math.max(maxRunning, running);
    try {
      await Promise.resolve();
      return script[probe.mock.calls.length - 1] ?? script[script.length - 1]!;
    } finally {
      running -= 1;
    }
  });
  return { probe, maxRunning: () => maxRunning };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('BackendHealthMonitor', () => {
  it('never overlaps probes: a slow probe blocks the next one', async () => {
    vi.useFakeTimers();
    let resolveProbe: ((result: BackendHealthProbeResult) => void) | null = null;
    let running = 0;
    let maxRunning = 0;
    const probe = vi.fn(() => {
      running += 1;
      maxRunning = Math.max(maxRunning, running);
      return new Promise<BackendHealthProbeResult>((resolve) => {
        resolveProbe = (result) => {
          running -= 1;
          resolve(result);
        };
      });
    });
    const push = vi.fn();
    const monitor = new BackendHealthMonitor(probe, push, { intervalMs: 100 });
    monitor.start();
    await vi.advanceTimersByTimeAsync(0);

    // 第一次 probe 立即发起但迟迟不结束；多个间隔过后仍不得发起第二次。
    expect(probe).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(probe).toHaveBeenCalledTimes(1);

    resolveProbe?.(OK);
    await vi.advanceTimersByTimeAsync(100);
    expect(probe).toHaveBeenCalledTimes(2);
    resolveProbe?.(OK);
    await vi.advanceTimersByTimeAsync(0);
    expect(maxRunning).toBe(1);

    monitor.stop();
  });

  it('uses the higher startup threshold inside the grace window', async () => {
    vi.useFakeTimers();
    const { probe, maxRunning } = scriptedProbe([
      OK, // t=0 先连上，才能观察到 true → false 跃迁
      fail('timeout'),
      fail('timeout'),
      fail('timeout'),
      OK,
      fail('unreachable'),
    ]);
    const push = vi.fn();
    const monitor = new BackendHealthMonitor(probe, push, {
      intervalMs: 100,
      startupGraceMs: 1_000,
      startupFailThreshold: 3,
      stableFailThreshold: 1,
    });
    monitor.start();
    await vi.advanceTimersByTimeAsync(0);

    // 宽限期内（t<1000）需要连续 3 次失败才判 disconnected。
    await vi.advanceTimersByTimeAsync(100); // t=100 fail 1
    await vi.advanceTimersByTimeAsync(100); // t=200 fail 2
    expect(push).toHaveBeenCalledTimes(1); // 只有 connected:true
    await vi.advanceTimersByTimeAsync(100); // t=300 fail 3 → disconnected
    expect(push).toHaveBeenCalledTimes(2);
    expect((push.mock.calls[1]![0] as BackendHealthStatusChange).connected).toBe(false);

    // 恢复成功 → 立即 connected。
    await vi.advanceTimersByTimeAsync(100); // t=400 OK
    expect(push).toHaveBeenCalledTimes(3);
    expect((push.mock.calls[2]![0] as BackendHealthStatusChange).connected).toBe(true);

    // 宽限期已过（t>1000）：稳定期阈值 1，一次失败立刻 disconnected。
    await vi.advanceTimersByTimeAsync(1_000); // t=1400 fail 1
    expect(push).toHaveBeenCalledTimes(4);
    expect(push.mock.calls[3]![0]).toMatchObject({
      connected: false,
      failureKind: 'unreachable',
    });
    expect(maxRunning()).toBe(1);
    monitor.stop();
  });

  it('markRestart reopens the grace window and resets the fail count', async () => {
    vi.useFakeTimers();
    const { probe } = scriptedProbe([OK, fail('timeout'), fail('timeout'), fail('timeout')]);
    const push = vi.fn();
    const monitor = new BackendHealthMonitor(probe, push, {
      intervalMs: 100,
      startupGraceMs: 1_000,
      startupFailThreshold: 5,
      stableFailThreshold: 1,
    });
    monitor.start();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(200); // t=200，宽限期内 2 次失败（阈值 5）不判死

    // Gateway 重建：宽限期与计数复位。新宽限期内一次失败按 startup 阈值 5 容忍；
    // 若宽限期没有重开，稳定期阈值 1 早已判 disconnected。
    monitor.markRestart();
    await vi.advanceTimersByTimeAsync(100); // t=300，新宽限期内第 1 次失败
    expect(push).toHaveBeenCalledTimes(1); // 仍只有最初的 connected:true
    monitor.stop();
  });

  it('forwards the failure kind and first-failure timestamp on disconnect', async () => {
    vi.useFakeTimers();
    const { probe } = scriptedProbe([OK, fail('timeout'), fail('timeout'), fail('timeout')]);
    const push = vi.fn();
    const monitor = new BackendHealthMonitor(probe, push, {
      intervalMs: 100,
      startupGraceMs: 0, // 直接稳定期
      stableFailThreshold: 3,
    });
    monitor.start();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(300);

    expect(push).toHaveBeenCalledTimes(2);
    const change = push.mock.calls[1]![0] as BackendHealthStatusChange;
    expect(change).toMatchObject({ connected: false, failureKind: 'timeout' });
    // 首次失败发生在 t=100（fake clock 下即当前时刻往前 200ms）。
    expect(change.since).toBe(Date.now() - 200);
    monitor.stop();
  });

  it('recovers immediately on success and clears failure metadata', async () => {
    vi.useFakeTimers();
    const { probe } = scriptedProbe([
      OK,
      fail('unreachable'),
      fail('unreachable'),
      fail('unreachable'),
      OK,
    ]);
    const push = vi.fn();
    const monitor = new BackendHealthMonitor(probe, push, {
      intervalMs: 100,
      startupGraceMs: 0,
      stableFailThreshold: 3,
    });
    monitor.start();
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(300); // 3 次失败 → disconnected

    await vi.advanceTimersByTimeAsync(100); // 成功 → 立即恢复
    expect(push).toHaveBeenCalledTimes(3);
    expect(push.mock.calls[2]![0]).toEqual({
      connected: true,
      components: { cron: { status: 'ok' } },
    });
    monitor.stop();
  });

  it('treats a throwing probe as unknown failure without breaking the loop', async () => {
    vi.useFakeTimers();
    let calls = 0;
    const probe = vi.fn(async () => {
      calls += 1;
      if (calls === 1) return OK;
      throw new Error('boom');
    });
    const push = vi.fn();
    const monitor = new BackendHealthMonitor(probe, push, {
      intervalMs: 100,
      startupGraceMs: 0,
      stableFailThreshold: 2,
    });
    monitor.start();
    await vi.advanceTimersByTimeAsync(300);

    expect(probe).toHaveBeenCalledTimes(4);
    expect(push.mock.calls[1]![0]).toMatchObject({
      connected: false,
      failureKind: 'unknown',
    });
    monitor.stop();
  });

  it('stop discards in-flight results and pending schedules', async () => {
    vi.useFakeTimers();
    const { probe } = scriptedProbe([OK, OK, OK]);
    const push = vi.fn();
    const monitor = new BackendHealthMonitor(probe, push, { intervalMs: 100 });
    monitor.start();
    await vi.advanceTimersByTimeAsync(0);
    monitor.stop();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(probe).toHaveBeenCalledTimes(1);
    expect(push).toHaveBeenCalledTimes(1);
  });
});
