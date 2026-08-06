import path from 'node:path';

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  configurePlaywrightBrowserRegistry,
  playwrightBrowserPlatformKey,
} from '../../src/main/browser/playwright-browser-runtime';

const originalOfficial = process.env.PLAYWRIGHT_BROWSERS_PATH;
const originalCrew = process.env.CREW_PLAYWRIGHT_BROWSERS_PATH;

afterEach(() => {
  vi.unstubAllEnvs();
  if (originalOfficial === undefined) delete process.env.PLAYWRIGHT_BROWSERS_PATH;
  else process.env.PLAYWRIGHT_BROWSERS_PATH = originalOfficial;
  if (originalCrew === undefined) delete process.env.CREW_PLAYWRIGHT_BROWSERS_PATH;
  else process.env.CREW_PLAYWRIGHT_BROWSERS_PATH = originalCrew;
});

describe('Playwright browser registry bootstrap', () => {
  it('uses a stable platform/architecture directory key', () => {
    expect(playwrightBrowserPlatformKey('darwin', 'arm64')).toBe('darwin-arm64');
    expect(playwrightBrowserPlatformKey('win32', 'x64')).toBe('win32-x64');
  });

  it('does not override the official Playwright registry variable', () => {
    vi.stubEnv('PLAYWRIGHT_BROWSERS_PATH', '/operator/registry');
    vi.stubEnv('CREW_PLAYWRIGHT_BROWSERS_PATH', '/crew/registry');

    expect(configurePlaywrightBrowserRegistry()).toBe('/operator/registry');
    expect(process.env.PLAYWRIGHT_BROWSERS_PATH).toBe('/operator/registry');
  });

  it('resolves Crew explicit registry before playwright-core loads', () => {
    delete process.env.PLAYWRIGHT_BROWSERS_PATH;
    vi.stubEnv('CREW_PLAYWRIGHT_BROWSERS_PATH', './relative-registry');

    const resolved = configurePlaywrightBrowserRegistry();

    expect(resolved).toBe(path.resolve('./relative-registry'));
    expect(process.env.PLAYWRIGHT_BROWSERS_PATH).toBe(resolved);
  });
});

