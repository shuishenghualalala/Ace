/**
 * D10 regression: bindSystemTab() returns a disposer that clears the 8s
 * overview refresh interval, and disposeSystemTab() clears it directly.
 *
 * vitest runs in the node environment, so we stub the minimal window/document
 * surface system-page.ts touches (setInterval / clearInterval / getElementById)
 * for the bind path. We do NOT exercise refreshBackendData (network) — only
 * the timer lifecycle, which is the bug.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

// Minimal stubs installed before importing the module each test.
function installDomStub(): { intervals: number[]; removed: number[] } {
  const intervals: number[] = [];
  const removed: number[] = [];
  let nextId = 1;
  const g = globalThis as unknown as Record<string, unknown>;
  g.window = {
    setInterval: vi.fn((_cb: TimerHandler, _ms?: number) => {
      intervals.push(nextId);
      return nextId++;
    }),
    clearInterval: vi.fn((id?: number) => {
      if (id !== undefined) removed.push(id);
    }),
    setTimeout: vi.fn(() => 0),
  };
  g.document = {
    getElementById: vi.fn(() => null),
    querySelector: vi.fn(() => null),
    querySelectorAll: vi.fn(() => []),
    addEventListener: vi.fn(),
  };
  return { intervals, removed };
}

function clearDomStub(): void {
  const g = globalThis as unknown as Record<string, unknown>;
  delete g.window;
  delete g.document;
}

describe('bindSystemTab / disposeSystemTab (D10: interval teardown)', () => {
  beforeEach(() => {
    // Reset modules so the module-level overviewRefreshTimer starts null.
    vi.resetModules();
    installDomStub();
  });

  afterEach(() => {
    clearDomStub();
    vi.restoreAllMocks();
  });

  it('bindSystemTab creates an interval and the returned disposer clears it', async () => {
    const { intervals, removed } = installDomStub();
    vi.resetModules();
    const mod = await import('../../src/ui/features/system-page');
    const dom = (globalThis as unknown as { window: any; document: any });

    // Force getElementById to return a stub element with addEventListener so
    // the refresh-button bind path doesn't short-circuit (interval still set).
    dom.document.getElementById = vi.fn(() => ({
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })) as any;

    const dispose = mod.bindSystemTab();
    expect(intervals.length).toBe(1);
    expect(removed.length).toBe(0);

    dispose();
    expect(removed).toContain(intervals[0]);
  });

  it('disposeSystemTab() is a no-op when no interval is running', async () => {
    installDomStub();
    vi.resetModules();
    const mod = await import('../../src/ui/features/system-page');
    expect(() => mod.disposeSystemTab()).not.toThrow();
  });
});
