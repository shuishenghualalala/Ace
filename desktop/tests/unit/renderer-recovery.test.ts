import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RendererRecovery } from '../../src/main/renderer-recovery';

const timings = { startupMs: 1_000, hangMs: 400, heartbeatMs: 800, stableMs: 5_000, retryDelaysMs: [10, 20] };

function fixture(watchHeartbeat?: () => boolean) {
  const callbacks = { record: vi.fn(), release: vi.fn(), restart: vi.fn(), exhausted: vi.fn(), watchHeartbeat };
  const recovery = new RendererRecovery(callbacks, timings);
  return { recovery, ...callbacks };
}

beforeEach(() => vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'performance'] }));
afterEach(() => vi.useRealTimers());

describe('main renderer recovery', () => {
  it('charges one retry for overlapping exit and load failures and fences the old document', () => {
    const { recovery, release, restart, record } = fixture();
    const first = recovery.begin();
    recovery.fail(first, { reason: 'oom', code: 13 });
    recovery.fail(first, { reason: 'load-failed', code: -6 });
    recovery.ready(first);
    expect(recovery.canServe(first)).toBe(false);
    expect(release).toHaveBeenCalledExactlyOnceWith(first);
    vi.advanceTimersByTime(10);
    expect(restart).toHaveBeenCalledExactlyOnceWith(first);

    const second = recovery.begin();
    recovery.fail(first, { reason: 'killed' });
    recovery.close(first);
    recovery.ready(second);
    expect(recovery.isReady(second)).toBe(true);
    expect(record).toHaveBeenCalledWith(expect.objectContaining({ event: 'renderer-failed', failure: { reason: 'oom', code: 13 } }));
  });

  it('exhausts repeated startup deadlines and grants a fresh budget on native retry', () => {
    const { recovery, restart, exhausted } = fixture();
    recovery.begin();
    vi.advanceTimersByTime(1_010);
    recovery.begin();
    vi.advanceTimersByTime(1_020);
    const third = recovery.begin();
    vi.advanceTimersByTime(2_000);
    expect(restart).toHaveBeenCalledTimes(2);
    expect(exhausted).toHaveBeenCalledExactlyOnceWith(third, { reason: 'startup-timeout' });

    recovery.retry(third);
    expect(restart).toHaveBeenCalledTimes(3);
    const fresh = recovery.begin();
    recovery.fail(fresh, { reason: 'crashed' });
    vi.advanceTimersByTime(10);
    expect(restart).toHaveBeenCalledTimes(4);
  });

  it('ignores transient unresponsiveness and does not extend a sustained hang on duplicate events', () => {
    const { recovery, release, restart } = fixture();
    const id = recovery.begin();
    recovery.ready(id);
    recovery.unresponsive(id);
    vi.advanceTimersByTime(300);
    recovery.responsive(id);
    vi.advanceTimersByTime(300);
    expect(recovery.isReady(id)).toBe(true);
    expect(release).not.toHaveBeenCalled();

    recovery.unresponsive(id);
    vi.advanceTimersByTime(300);
    recovery.unresponsive(id);
    vi.advanceTimersByTime(110);
    expect(release).toHaveBeenCalledExactlyOnceWith(id);
    expect(restart).toHaveBeenCalledExactlyOnceWith(id);
  });

  it('recovers a foreground renderer with no heartbeat even if Electron reports no native hang', () => {
    let foreground = false;
    const { recovery, release, restart, record } = fixture(() => foreground);
    const id = recovery.begin();
    recovery.ready(id);
    vi.advanceTimersByTime(2_000);
    expect(release).not.toHaveBeenCalled();

    foreground = true;
    recovery.heartbeat(id);
    vi.advanceTimersByTime(600);
    recovery.heartbeat(id);
    vi.advanceTimersByTime(600);
    expect(release).not.toHaveBeenCalled();

    recovery.heartbeat(id - 1);
    vi.advanceTimersByTime(210);
    expect(restart).toHaveBeenCalledExactlyOnceWith(id);
    expect(record).toHaveBeenCalledWith(expect.objectContaining({
      event: 'renderer-failed', failure: { reason: 'unresponsive', signal: 'heartbeat' },
    }));
  });

  it('grants a fresh heartbeat interval after the main process wakes from a long pause', () => {
    let now = 0;
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => now);
    const { recovery, release } = fixture(() => true);
    try {
      const id = recovery.begin();
      recovery.ready(id);
      now += 60_000;
      vi.advanceTimersByTime(200);
      expect(release).not.toHaveBeenCalled();
      for (let i = 0; i < 3; i++) {
        now += 200;
        vi.advanceTimersByTime(200);
      }
      expect(release).not.toHaveBeenCalled();
      now += 200;
      vi.advanceTimersByTime(200);
      expect(release).toHaveBeenCalledExactlyOnceWith(id);
    } finally {
      clock.mockRestore();
    }
  });

  it('replenishes the budget after sustained readiness and cancels timers on close', () => {
    const { recovery, restart, exhausted } = fixture();
    recovery.fail(recovery.begin(), { reason: 'crashed' });
    vi.advanceTimersByTime(10);
    const stable = recovery.begin();
    recovery.ready(stable);
    vi.advanceTimersByTime(5_000);
    recovery.fail(stable, { reason: 'memory-eviction' });
    vi.advanceTimersByTime(10);
    expect(restart).toHaveBeenCalledTimes(2);
    const closed = recovery.begin();
    recovery.close(closed);
    vi.advanceTimersByTime(10_000);
    expect(exhausted).not.toHaveBeenCalled();
    expect(restart).toHaveBeenCalledTimes(2);
  });

  it('routes integrity failure directly to the native fallback and budgets creation failures', () => {
    const { recovery, restart, exhausted } = fixture();
    const broken = recovery.begin();
    recovery.fail(broken, { reason: 'integrity-failure' });
    expect(restart).not.toHaveBeenCalled();
    expect(exhausted).toHaveBeenCalledWith(broken, { reason: 'integrity-failure' });
    recovery.retry(broken);
    recovery.replacementFailed(broken);
    vi.advanceTimersByTime(10);
    recovery.replacementFailed(broken);
    vi.advanceTimersByTime(20);
    recovery.replacementFailed(broken);
    expect(restart).toHaveBeenCalledTimes(3);
    expect(exhausted).toHaveBeenLastCalledWith(broken, { reason: 'launch-failed' });
  });
});
