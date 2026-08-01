/**
 * Playwright config for Crew desktop visual regression tests.
 *
 * IMPORTANT: this config is for Electron-style desktop testing. The
 * renderer is a static file:// URL served from dist/. We use Playwright
 * in a way that loads the built renderer into a headless Chromium so
 * that we can capture consistent screenshots.
 *
 * This suite is opt-in and is not part of the default `npm run check` pipeline.
 *
 * To capture baselines:
 *   npx playwright test --update-snapshots
 *
 * To verify no drift:
 *   npx playwright test
 */
import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const visualTestDir = __dirname;
const desktopRoot = path.resolve(visualTestDir, '../..');
const desktopBaseUrl = pathToFileURL(`${desktopRoot}${path.sep}`).href;

export default defineConfig({
  testDir: './specs',
  // Visual tests are slow (Electron + screenshots). Allow extra time.
  timeout: 60_000,
  expect: {
    timeout: 10_000,
    toHaveScreenshot: {
      // The renderer has small animation jitter (fontconfig differs
      // between hosts). Allow a 0.1% pixel diff before failing.
      maxDiffPixels: 0.001,
    },
  },
  // Run tests serially: a single renderer at a time, deterministic order.
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: desktopBaseUrl,
    trace: 'retain-on-failure',
    // Desktop is 1920x1080. Don't shrink to fit — we want exact pixels.
    viewport: { width: 1920, height: 1080 },
    // Disable animations for stable screenshots.
    launchOptions: {
      args: ['--disable-blink-features=AutomationControlled'],
    },
  },
  projects: [
    {
      name: 'desktop-renderer',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
