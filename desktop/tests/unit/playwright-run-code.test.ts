import { describe, expect, it } from 'vitest';

import {
  executeUnsafePlaywrightCode,
  RunCodeTimeoutError,
} from '../../src/main/browser/playwright-run-code';

import type { Page } from '../../src/main/browser/playwright-compat';

function options(
  timeoutMs = 1_000,
  withCompletion = async <T>(action: () => Promise<T>): Promise<T> => await action(),
  onTimeout?: () => Promise<void>,
) {
  return {
    deadlineAt: Date.now() + timeoutMs,
    withCompletion,
    ...(onTimeout ? { onTimeout } : {}),
  };
}

describe('browser_run_code_unsafe VM', () => {
  it('exposes page/__end__ only and returns the exact JSON.stringify result', async () => {
    let completions = 0;
    const page = {
      title: async (): Promise<string> => 'A `quoted` title',
    } as unknown as Page;
    const result = await executeUnsafePlaywrightCode(
      page,
      `async (page) => ({
        title: await page.title(),
        processType: typeof process,
        requireType: typeof require,
        pageInjected: typeof globalThis.page,
        endInjected: typeof globalThis.__end__,
      })`,
      options(1_000, async <T>(action: () => Promise<T>): Promise<T> => {
        completions += 1;
        return await action();
      }),
    );

    expect(result).toBe(JSON.stringify({
      title: 'A `quoted` title',
      processType: 'undefined',
      requireType: 'undefined',
      pageInjected: 'object',
      endInjected: 'object',
    }));
    expect(completions).toBe(1);
  });

  it('matches JSON.stringify undefined and rejects non-serializable results', async () => {
    const page = {} as Page;
    await expect(executeUnsafePlaywrightCode(
      page,
      'async () => undefined',
      options(),
    )).resolves.toBeUndefined();
    await expect(executeUnsafePlaywrightCode(
      page,
      'async () => { const value = {}; value.self = value; return value; }',
      options(),
    )).rejects.toThrow(/circular/i);
  });

  it('interrupts synchronous loops and bounds asynchronous hangs', async () => {
    const page = {
      never: new Promise<never>(() => {}),
    } as unknown as Page;
    await expect(executeUnsafePlaywrightCode(
      page,
      '() => { while (true) {} }',
      options(25),
    )).rejects.toBeInstanceOf(RunCodeTimeoutError);
    await expect(executeUnsafePlaywrightCode(
      page,
      'async page => await page.never',
      options(25),
    )).rejects.toBeInstanceOf(RunCodeTimeoutError);
  });

  it('surfaces active route-style unhandled rejections without poisoning later runs', async () => {
    const page = {
      triggerRouteFailure: (): Promise<void> => {
        void Promise.reject(new Error('route callback exploded'));
        return new Promise((resolve) => setTimeout(resolve, 100));
      },
    } as unknown as Page;
    await expect(executeUnsafePlaywrightCode(
      page,
      'async page => await page.triggerRouteFailure()',
      options(),
    )).rejects.toThrow('route callback exploded');

    await expect(executeUnsafePlaywrightCode(
      page,
      'async () => 42',
      options(),
    )).resolves.toBe('42');
  });

  it('keeps the process usable when a long-lived callback rejects after return', async () => {
    const page = {
      installLateFailure: (): void => {
        setImmediate(() => {
          void Promise.reject(new Error('late route callback exploded'));
        });
      },
    } as unknown as Page;
    await expect(executeUnsafePlaywrightCode(
      page,
      'async page => { page.installLateFailure(); return "installed"; }',
      options(),
    )).resolves.toBe('"installed"');
    await new Promise((resolve) => setTimeout(resolve, 25));
    await expect(executeUnsafePlaywrightCode(
      page,
      'async () => "still alive"',
      options(),
    )).resolves.toBe('"still alive"');
  });

  it('attributes concurrent unhandled rejections to the originating run only', async () => {
    const failingPage = {
      begin: async (): Promise<void> => {
        setTimeout(() => {
          void Promise.reject(new Error('owner-a route exploded'));
        }, 10);
        await new Promise((resolve) => setTimeout(resolve, 80));
      },
    } as unknown as Page;
    const healthyPage = {
      begin: async (): Promise<string> => {
        await new Promise((resolve) => setTimeout(resolve, 40));
        return 'owner-b healthy';
      },
    } as unknown as Page;

    const [ownerA, ownerB] = await Promise.allSettled([
      executeUnsafePlaywrightCode(
        failingPage,
        'async page => { await page.begin(); return "owner-a"; }',
        options(),
      ),
      executeUnsafePlaywrightCode(
        healthyPage,
        'async page => await page.begin()',
        options(),
      ),
    ]);

    expect(ownerA).toMatchObject({
      status: 'rejected',
      reason: expect.objectContaining({ message: 'owner-a route exploded' }),
    });
    expect(ownerB).toEqual({
      status: 'fulfilled',
      value: '"owner-b healthy"',
    });
  });

  it('preserves Playwright EventEmitter listener this as the public façade', async () => {
    const rawPage: Record<string, unknown> = {
      on: (
        _event: string,
        listener: (this: unknown, value: string) => void,
      ): unknown => {
        setImmediate(() => {
          Reflect.apply(listener, rawPage, ['ready']);
        });
        return rawPage;
      },
      off: (): unknown => rawPage,
    };

    await expect(executeUnsafePlaywrightCode(
      rawPage as unknown as Page,
      `async page => await new Promise(resolve => {
        page.on('ready', function(value) {
          resolve({ samePage: this === page, value });
        });
      })`,
      options(),
    )).resolves.toBe('{"samePage":true,"value":"ready"}');
  });

  it('does not reload the page when only a client-side wait crosses the deadline', async () => {
    let recoveries = 0;
    const page = {
      waitForTimeout: async (timeoutMs: number): Promise<void> => {
        await new Promise((resolve) => setTimeout(resolve, timeoutMs));
      },
    } as unknown as Page;

    await expect(executeUnsafePlaywrightCode(
      page,
      'async page => await page.waitForTimeout(40)',
      options(10, undefined, async () => {
        recoveries += 1;
      }),
    )).rejects.toBeInstanceOf(RunCodeTimeoutError);
    expect(recoveries).toBe(0);
  });

  it('revokes the retained Page/Locator/Context/Browser graph before late dispatch', async () => {
    const calls: string[] = [];
    class FakeBrowser {
      async newContext(): Promise<object> {
        calls.push('newContext');
        return { close: async (): Promise<void> => {} };
      }
    }
    const browser = new FakeBrowser();
    const context = {
      browser: (): FakeBrowser => browser,
      newPage: async (): Promise<object> => {
        calls.push('newPage');
        return { close: async (): Promise<void> => {} };
      },
    };
    const locator = {
      click: async (): Promise<void> => {
        calls.push('click');
      },
    };
    const page = {
      context: (): object => context,
      locator: (): object => locator,
      evaluate: async (): Promise<void> => {
        calls.push('evaluate');
      },
      close: async (): Promise<void> => {
        calls.push('close');
      },
      scheduleLate: (callback: () => void): void => {
        setTimeout(callback, 50);
      },
      waitForever: (): Promise<never> => new Promise(() => {}),
    } as unknown as Page;
    let recoveries = 0;

    await expect(executeUnsafePlaywrightCode(
      page,
      `async page => {
        const locator = page.locator('#late');
        const context = page.context();
        const browser = context.browser();
        page.scheduleLate(() => {
          try { void page.evaluate(() => document.body.dataset.late = 'yes'); } catch {}
          try { void locator.click(); } catch {}
          try { void context.newPage(); } catch {}
          try { void browser.newContext(); } catch {}
          try { void page.close(); } catch {}
        });
        await page.waitForever();
      }`,
      options(20, undefined, async () => {
        recoveries += 1;
      }),
    )).rejects.toBeInstanceOf(RunCodeTimeoutError);
    await new Promise((resolve) => setTimeout(resolve, 80));

    expect(calls).toEqual([]);
    expect(recoveries).toBe(1);
  });

  it('runs timeout recovery before returning and joins dispatched evaluate/action work', async () => {
    let cancelled = false;
    let evaluateMutations = 0;
    let clicks = 0;
    const delayed = (mutation: () => void): Promise<void> => (
      new Promise((resolve) => {
        setTimeout(() => {
          if (!cancelled) mutation();
          resolve();
        }, 70);
      })
    );
    const page = {
      evaluate: async (): Promise<void> => await delayed(() => {
        evaluateMutations += 1;
      }),
      locator: (): object => ({
        click: async (): Promise<void> => await delayed(() => {
          clicks += 1;
        }),
      }),
      title: async (): Promise<string> => 'still usable',
    } as unknown as Page;

    await expect(executeUnsafePlaywrightCode(
      page,
      `async page => await Promise.all([
        page.evaluate(() => new Promise(() => {})),
        page.locator('#late').click(),
      ])`,
      options(20, undefined, async () => {
        cancelled = true;
      }),
    )).rejects.toBeInstanceOf(RunCodeTimeoutError);

    expect(evaluateMutations).toBe(0);
    expect(clicks).toBe(0);
    await expect(executeUnsafePlaywrightCode(
      page,
      'async page => await page.title()',
      options(),
    )).resolves.toBe('"still usable"');
  });

  it('bounds Locator actionability by the command deadline before recovery navigation', async () => {
    let observedTimeout = 0;
    let recoveries = 0;
    const locator = {
      count: async (): Promise<number> => 1,
      click: async (options?: { timeout?: number }): Promise<void> => {
        observedTimeout = Number(options?.timeout ?? 0);
        await new Promise((resolve) => setTimeout(resolve, observedTimeout));
        const error = new Error(`Timeout ${observedTimeout}ms exceeded`);
        error.name = 'TimeoutError';
        throw error;
      },
    };
    const page = {
      locator: (): object => locator,
    } as unknown as Page;

    await expect(executeUnsafePlaywrightCode(
      page,
      `async page => {
        await page.locator('#eventually-visible').click();
      }`,
      options(150, undefined, async () => {
        recoveries += 1;
      }),
    )).rejects.toBeInstanceOf(RunCodeTimeoutError);
    expect(observedTimeout).toBeGreaterThan(0);
    expect(observedTimeout).toBeLessThan(150);
    expect(recoveries).toBe(1);
  });

  it('keeps a successfully installed route handler live and removes timed-out handlers', async () => {
    const handlers = new Map<string, (route: object) => unknown>();
    const removed: string[] = [];
    const page = {
      route: async (
        pattern: string,
        handler: (route: object) => unknown,
      ): Promise<void> => {
        handlers.set(pattern, handler);
      },
      unroute: async (
        pattern: string,
        handler: (route: object) => unknown,
      ): Promise<void> => {
        if (handlers.get(pattern) === handler) handlers.delete(pattern);
        removed.push(pattern);
      },
      hang: (): Promise<never> => new Promise(() => {}),
    } as unknown as Page;

    await expect(executeUnsafePlaywrightCode(
      page,
      `async page => {
        await page.route('**/persistent', async route => {
          await route.fulfill({ status: 200, body: 'installed' });
        });
        return 'installed';
      }`,
      options(),
    )).resolves.toBe('"installed"');
    const fulfilled: unknown[] = [];
    await handlers.get('**/persistent')?.({
      fulfill: async (options: unknown): Promise<void> => {
        fulfilled.push(options);
      },
    });
    expect(fulfilled).toEqual([{ status: 200, body: 'installed' }]);

    await expect(executeUnsafePlaywrightCode(
      page,
      `async page => {
        await page.route('**/timed-out', async route => {
          await route.fulfill({ status: 200, body: 'late' });
        });
        await page.hang();
      }`,
      options(20),
    )).rejects.toBeInstanceOf(RunCodeTimeoutError);
    expect(handlers.has('**/persistent')).toBe(true);
    expect(handlers.has('**/timed-out')).toBe(false);
    expect(removed).toContain('**/timed-out');
  });

  it('turns a WebSocket route from a failed snippet into transparent passthrough', async () => {
    const handlers = new Map<string, (route: object) => unknown>();
    const page = {
      routeWebSocket: async (
        pattern: string,
        handler: (route: object) => unknown,
      ): Promise<void> => {
        handlers.set(pattern, handler);
      },
      hang: (): Promise<never> => new Promise(() => {}),
    } as unknown as Page;

    await expect(executeUnsafePlaywrightCode(
      page,
      `async page => {
        await page.routeWebSocket('**/stale', async route => {
          await route.close();
        });
        await page.hang();
      }`,
      options(20),
    )).rejects.toBeInstanceOf(RunCodeTimeoutError);

    let connected = 0;
    let closed = 0;
    await handlers.get('**/stale')?.({
      connectToServer: async (): Promise<void> => {
        connected += 1;
      },
      close: async (): Promise<void> => {
        closed += 1;
      },
    });
    expect(connected).toBe(1);
    expect(closed).toBe(0);

    await expect(executeUnsafePlaywrightCode(
      page,
      `async page => {
        await page.routeWebSocket('**/live', async route => {
          await route.close();
        });
        return 'installed';
      }`,
      options(),
    )).resolves.toBe('"installed"');
    await handlers.get('**/live')?.({
      connectToServer: async (): Promise<void> => {
        connected += 1;
      },
      close: async (): Promise<void> => {
        closed += 1;
      },
    });
    expect(connected).toBe(1);
    expect(closed).toBe(1);
  });
});
