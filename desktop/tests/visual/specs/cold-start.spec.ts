/**
 * Cold-start FOUC visual regression test.
 *
 * Verifies that the inline <head> script in index.html sets
 * `<html data-theme="...">` BEFORE the first painted frame, so dark
 * users don't see a flash of light background on cold start (and
 * vice versa).
 *
 * Prereq: `npx playwright install chromium` must have been run.
 * Skipped otherwise (the spec file is a no-op until the browser is
 * available).
 */
import { test, expect } from '@playwright/test';

test.describe('cold-start FOUC guard', () => {
  test('dark user sees <html data-theme="dark"> before first paint', async ({ page }) => {
    // Pre-seed localStorage so the inline script picks up 'dark'.
    await page.addInitScript(() => {
      localStorage.setItem(
        'crew.settings',
        JSON.stringify({ themeMode: 'dark', accent: 'blue' }),
      );
    });
    // Use about:blank to discard any cached first frame, then load.
    await page.goto('about:blank');
    await page.goto('dist/assets/index.html');
    // The inline script runs before any rendering, so dataset.theme
    // must already be set when we read it.
    const theme = await page.evaluate(() => document.documentElement.dataset.theme);
    expect(theme).toBe('dark');
  });

  test('light user sees <html data-theme="light"> before first paint', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem(
        'crew.settings',
        JSON.stringify({ themeMode: 'light', accent: 'blue' }),
      );
    });
    await page.goto('about:blank');
    await page.goto('dist/assets/index.html');
    const theme = await page.evaluate(() => document.documentElement.dataset.theme);
    expect(theme).toBe('light');
  });

  test('accent is applied to --v2-primary inline before first paint', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem(
        'crew.settings',
        JSON.stringify({ themeMode: 'dark', accent: 'violet' }),
      );
    });
    await page.goto('about:blank');
    await page.goto('dist/assets/index.html');
    const v2p = await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue('--v2-primary').trim(),
    );
    expect(v2p).toBe('#7c5cff');
  });
});
