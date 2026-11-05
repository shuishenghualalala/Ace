import { describe, expect, it, vi } from 'vitest';
import { PageRegistry, type PageContribution } from '../../src/ui/features/page-registry';

function contribution(id: string, overrides: Partial<PageContribution> = {}): PageContribution {
  return {
    id,
    activate: vi.fn(async () => undefined),
    deactivate: vi.fn(async () => undefined),
    ...overrides,
  };
}

describe('PageRegistry', () => {
  it('deactivates the previous page before activating the next one and aborts its signal', async () => {
    const registry = new PageRegistry();
    let firstSignal: AbortSignal | undefined;
    const first = contribution('wiki', {
      activate: vi.fn(async ({ signal }) => { firstSignal = signal; }),
    });
    const second = contribution('chat');
    registry.register(first);
    registry.register(second);

    await registry.activate('wiki');
    expect(registry.activeId).toBe('wiki');
    await registry.activate('chat');

    expect(firstSignal?.aborted).toBe(true);
    expect(first.deactivate).toHaveBeenCalledTimes(1);
    expect(second.activate).toHaveBeenCalledTimes(1);
    expect(registry.activeId).toBe('chat');
  });

  it('returns false for an unknown page without disturbing the active page', async () => {
    const registry = new PageRegistry();
    const page = contribution('wiki');
    registry.register(page);

    await registry.activate('wiki');
    expect(await registry.activate('missing')).toBe(false);

    await vi.waitFor(() => expect(page.deactivate).toHaveBeenCalledTimes(1));
    expect(registry.activeId).toBeNull();
  });

  it('rolls back a failed activation after aborting the failed page', async () => {
    const registry = new PageRegistry();
    let signal: AbortSignal | undefined;
    const failing = contribution('wiki', {
      activate: vi.fn(async (context) => {
        signal = context.signal;
        throw new Error('activation failed');
      }),
    });
    registry.register(failing);

    await expect(registry.activate('wiki')).rejects.toThrow('activation failed');
    expect(signal?.aborted).toBe(true);
    expect(registry.activeId).toBeNull();
  });

  it('keeps a contribution registered when an old disposer runs after replacement', async () => {
    const registry = new PageRegistry();
    const first = contribution('wiki');
    const disposeFirst = registry.register(first);
    await registry.activate('wiki');
    await registry.deactivate();
    expect(first.deactivate).toHaveBeenCalledTimes(1);

    // Remove the first registration, then invoke its already-owned disposer
    // after installing a replacement. The late call must be harmless.
    disposeFirst();
    const replacement = contribution('wiki');
    registry.register(replacement);
    disposeFirst();
    expect(await registry.activate('wiki')).toBe(true);
    expect(replacement.activate).toHaveBeenCalledTimes(1);
  });

  it('makes a disposer idempotent while awaiting asynchronous deactivation', async () => {
    const registry = new PageRegistry();
    let release: (() => void) | undefined;
    const deactivated = new Promise<void>((resolve) => { release = resolve; });
    const page = contribution('wiki', { deactivate: vi.fn(() => deactivated) });
    const dispose = registry.register(page);
    await registry.activate('wiki');

    const firstDispose = dispose();
    const secondDispose = dispose();
    let settled = false;
    void firstDispose.then(() => { settled = true; });
    await Promise.resolve();
    expect(settled).toBe(false);
    expect(page.deactivate).toHaveBeenCalledTimes(1);
    release?.();
    await Promise.all([firstDispose, secondDispose]);
    expect(settled).toBe(true);
    expect(registry.activeId).toBeNull();
  });

  it('does not let an older concurrent activation become active after a newer one', async () => {
    const registry = new PageRegistry();
    let releaseWiki: (() => void) | undefined;
    const wikiReady = new Promise<void>((resolve) => { releaseWiki = resolve; });
    const wiki = contribution('wiki', { activate: vi.fn(() => wikiReady) });
    const chat = contribution('chat');
    registry.register(wiki);
    registry.register(chat);

    const wikiActivation = registry.activate('wiki');
    const chatActivation = registry.activate('chat');
    releaseWiki?.();
    await Promise.all([wikiActivation, chatActivation]);

    expect(registry.activeId).toBe('chat');
  });

  it('waits for deferred cleanup before activating the next page', async () => {
    const registry = new PageRegistry();
    let releaseDeactivate!: () => void;
    const cleanup = new Promise<void>((resolve) => { releaseDeactivate = resolve; });
    let firstSignal!: AbortSignal;
    const first = contribution('wiki', {
      activate: vi.fn(async ({ signal }) => { firstSignal = signal; }),
      deactivate: vi.fn(() => cleanup),
    });
    const second = contribution('chat');
    registry.register(first);
    registry.register(second);
    await registry.activate('wiki');

    const next = registry.activate('chat');
    await Promise.resolve();
    expect(firstSignal.aborted).toBe(true);
    expect(second.activate).not.toHaveBeenCalled();
    releaseDeactivate();
    await next;
    expect(second.activate).toHaveBeenCalledTimes(1);
  });

  it('waits for failed activation cleanup before rejecting', async () => {
    const registry = new PageRegistry();
    let releaseCleanup!: () => void;
    const cleanup = new Promise<void>((resolve) => { releaseCleanup = resolve; });
    const failing = contribution('wiki', {
      activate: vi.fn(async () => { throw new Error('activation failed'); }),
      deactivate: vi.fn(() => cleanup),
    });
    registry.register(failing);
    let settled = false;
    const activation = registry.activate('wiki').catch((error) => {
      settled = true;
      throw error;
    });
    await Promise.resolve();
    expect(settled).toBe(false);
    releaseCleanup();
    await expect(activation).rejects.toThrow('activation failed');
    expect(failing.deactivate).toHaveBeenCalledTimes(1);
  });

  it('does not reactivate Wiki when a same-tick navigation ends at an unknown page', async () => {
    const registry = new PageRegistry();
    const wiki = contribution('wiki');
    registry.register(wiki);
    const wikiActivation = registry.activate('wiki');
    const unknownActivation = registry.activate('missing');
    await Promise.all([wikiActivation, unknownActivation]);
    expect(registry.activeId).toBeNull();
    expect(wiki.activate).not.toHaveBeenCalled();
  });
});
