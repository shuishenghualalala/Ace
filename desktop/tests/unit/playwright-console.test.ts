import { describe, expect, it, vi } from 'vitest';

import {
  clearConsoleMessages,
  readConsoleMessages,
  shouldIncludeConsoleMessage,
} from '../../src/main/browser/playwright-console';

import type { Page } from '../../src/main/browser/playwright-compat';

function message(
  type: string,
  text: string,
  url = 'https://example.test/app.js',
  lineNumber = 7,
): Record<string, unknown> {
  return {
    type: () => type,
    text: () => text,
    location: () => ({
      url,
      line: lineNumber,
      column: 0,
      lineNumber,
      columnNumber: 0,
    }),
  };
}

describe('Playwright console parity', () => {
  it('uses upstream severity mapping, since-navigation counts and page-error stacks', async () => {
    const pageError = new Error('uncaught-page-error');
    pageError.stack = 'Error: uncaught-page-error\n    at app.js:19:3';
    const retained = [
      message('error', 'console-error'),
      message('warning', 'console-warning'),
      message('log', 'console-info'),
      message('debug', 'console-debug'),
      message('assert', 'console-assert'),
    ];
    const page = {
      consoleMessages: vi.fn(async () => retained),
      pageErrors: vi.fn(async () => [pageError]),
    } as unknown as Page;

    const result = await readConsoleMessages(page, {
      level: 'warning',
      all: false,
    });

    expect(result).toMatchObject({
      format: 'text',
      extension: 'log',
      total: 6,
      errors: 2,
      warnings: 1,
      returned: 4,
    });
    expect(result.text).toBe([
      'Total messages: 6 (Errors: 2, Warnings: 1)',
      'Returning 4 messages for level "warning"',
      '',
      '[ERROR] console-error @ https://example.test/app.js:7',
      '[WARNING] console-warning @ https://example.test/app.js:7',
      '[ASSERT] console-assert @ https://example.test/app.js:7',
      'Error: uncaught-page-error\n    at app.js:19:3',
    ].join('\n'));
    expect(page.consoleMessages).toHaveBeenNthCalledWith(1, {
      filter: 'since-navigation',
    });
    expect(page.consoleMessages).toHaveBeenNthCalledWith(2, {
      filter: 'since-navigation',
    });
    expect(page.pageErrors).toHaveBeenNthCalledWith(1, {
      filter: 'since-navigation',
    });
    expect(page.pageErrors).toHaveBeenNthCalledWith(2, {
      filter: 'since-navigation',
    });
  });

  it('passes all to both retained buffers while keeping the upstream current-page count', async () => {
    const current = [message('log', 'after-navigation')];
    const all = [
      message('debug', 'before-navigation'),
      ...current,
    ];
    const oldError = new Error('old-page-error');
    const page = {
      consoleMessages: vi.fn(async ({ filter }: { filter: string }) => (
        filter === 'all' ? all : current
      )),
      pageErrors: vi.fn(async ({ filter }: { filter: string }) => (
        filter === 'all' ? [oldError] : []
      )),
    } as unknown as Page;

    const result = await readConsoleMessages(page, {
      level: 'debug',
      all: true,
    });

    expect(result.total).toBe(1);
    expect(result.returned).toBe(3);
    expect(result.text).toContain('Returning 3 messages for level "debug"');
    expect(result.text).toContain('[DEBUG] before-navigation');
    expect(result.text).toContain('[LOG] after-navigation');
    expect(result.text).toContain('Error: old-page-error');
    expect(page.consoleMessages).toHaveBeenLastCalledWith({ filter: 'all' });
    expect(page.pageErrors).toHaveBeenLastCalledWith({ filter: 'all' });
  });

  it('clears both public Playwright retention buffers', async () => {
    const page = {
      clearConsoleMessages: vi.fn(async () => undefined),
      clearPageErrors: vi.fn(async () => undefined),
    } as unknown as Page;

    await clearConsoleMessages(page);

    expect(page.clearConsoleMessages).toHaveBeenCalledOnce();
    expect(page.clearPageErrors).toHaveBeenCalledOnce();
  });

  it('maps all Chromium console types to cumulative upstream levels', () => {
    expect(shouldIncludeConsoleMessage('error', 'assert')).toBe(true);
    expect(shouldIncludeConsoleMessage('error', 'warning')).toBe(false);
    expect(shouldIncludeConsoleMessage('warning', 'warning')).toBe(true);
    expect(shouldIncludeConsoleMessage('warning', 'log')).toBe(false);
    expect(shouldIncludeConsoleMessage('info', 'timeEnd')).toBe(true);
    expect(shouldIncludeConsoleMessage('info', 'trace')).toBe(false);
    expect(shouldIncludeConsoleMessage('debug', 'trace')).toBe(true);
  });
});
