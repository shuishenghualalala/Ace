import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DesktopShutdown, waitForShutdownTask } from '../../src/main/desktop-shutdown';

function pending() {
  return Promise.withResolvers<void>();
}

function harness(prepare: () => Promise<void>) {
  const exit = vi.fn();
  const resume = vi.fn();
  const report = vi.fn();
  const run = vi.fn(prepare);
  const shutdown = new DesktopShutdown({ prepare: run, exit, resume, report });
  return {
    shutdown, run, exit, resume, report,
    quit() {
      const event = { preventDefault: vi.fn() };
      shutdown.beforeQuit(event);
      return event.preventDefault;
    },
  };
}

describe('Desktop shutdown', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('vetoes native quit until the shared preparation settles and exits once', async () => {
    const gate = pending();
    const { shutdown, run, quit, exit } = harness(() => gate.promise);
    expect(quit()).toHaveBeenCalledOnce();
    expect(quit()).toHaveBeenCalledOnce();
    expect(shutdown.beginHandoff('update')).toBeNull();
    await vi.advanceTimersByTimeAsync(50);
    expect(run).toHaveBeenCalledExactlyOnceWith('app-quit');
    expect(exit).not.toHaveBeenCalled();
    expect(shutdown.canCloseWindows).toBe(false);

    gate.resolve();
    await vi.runAllTimersAsync();
    expect(exit).toHaveBeenCalledOnce();
    expect(shutdown.canCloseWindows).toBe(true);
    expect(quit()).not.toHaveBeenCalled();
  });

  it('does not exit ahead of an in-flight installer handoff', async () => {
    const { shutdown, run, quit, exit, resume } = harness(async () => {});
    await shutdown.beginHandoff('update');
    expect(quit()).toHaveBeenCalledOnce();
    expect(shutdown.beginHandoff('uninstall')).toBeNull();
    await vi.runAllTimersAsync();
    expect(run).toHaveBeenCalledExactlyOnceWith('update');
    expect(exit).not.toHaveBeenCalled();

    shutdown.finishHandoff(true);
    await vi.runAllTimersAsync();
    expect(exit).toHaveBeenCalledOnce();
    expect(resume).not.toHaveBeenCalled();
  });

  it('recovers from a rejected handoff and permits a new attempt', async () => {
    const { shutdown, run, exit, resume } = harness(async () => {});
    await shutdown.beginHandoff('update');
    shutdown.finishHandoff(false);
    expect(resume).toHaveBeenCalledOnce();
    expect(shutdown.canCloseWindows).toBe(false);
    expect(exit).not.toHaveBeenCalled();
    await shutdown.beginHandoff('uninstall');
    shutdown.finishHandoff(true);
    await vi.runAllTimersAsync();
    expect(run.mock.calls).toEqual([['update'], ['uninstall']]);
    expect(exit).toHaveBeenCalledOnce();
  });

  it('honors a pending native quit after an unsuccessful handoff', async () => {
    const failure = new Error('owned Gateway did not drain');
    const { shutdown, quit, exit, resume, report } = harness(async () => { throw failure; });
    const handoff = shutdown.beginHandoff('uninstall');
    expect(quit()).toHaveBeenCalledOnce();
    await expect(handoff).rejects.toBe(failure);
    shutdown.finishHandoff(false);
    await vi.runAllTimersAsync();
    expect(report).toHaveBeenCalledExactlyOnceWith(failure);
    expect(exit).toHaveBeenCalledOnce();
    expect(resume).not.toHaveBeenCalled();
  });

  it('releases a deadline timer when done and rejects when a release never settles', async () => {
    await expect(waitForShutdownTask(Promise.resolve('closed'), 'owner', 25)).resolves.toBe('closed');
    expect(vi.getTimerCount()).toBe(0);
    const stalled = pending();
    const timeout = expect(waitForShutdownTask(stalled.promise, 'owner', 25)).rejects.toThrow('owner');
    await vi.advanceTimersByTimeAsync(25);
    await timeout;
    expect(vi.getTimerCount()).toBe(0);
    stalled.reject(new Error('late cleanup error'));
  });
});
