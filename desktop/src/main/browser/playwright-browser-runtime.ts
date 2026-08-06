/**
 * Resolve the hermetic Playwright browser registry before playwright-core is loaded.
 *
 * playwright-core snapshots PLAYWRIGHT_BROWSERS_PATH while its package root is
 * evaluated. Setting the variable later may make an explicit Chromium executable
 * work, but recordVideo still looks for FFmpeg in the old registry. Keep this tiny
 * module dependency-free from playwright-core so playwright-compat can run it first.
 */

import { existsSync } from 'node:fs';
import path from 'node:path';

// Keep this non-dot-prefixed: packaging glob implementations differ on
// whether `**/*` includes dotfiles.
const STAGED_BROWSER_MARKER = 'crew-playwright-version.json';

export function playwrightBrowserPlatformKey(
  platform: NodeJS.Platform = process.platform,
  architecture: string = process.arch,
): string {
  return `${platform}-${architecture}`;
}

function stagedRegistryCandidates(): string[] {
  const key = playwrightBrowserPlatformKey();
  const candidates: string[] = [];
  const resourcesPath = (
    process as NodeJS.Process & { resourcesPath?: unknown }
  ).resourcesPath;
  if (typeof resourcesPath === 'string' && resourcesPath) {
    candidates.push(path.join(resourcesPath, 'playwright-browsers', key));
  }

  // `electron .` normally starts in desktop/. Vitest and repository-level
  // probes may start one directory above it, so support both without relying
  // on bundled __dirname (which changes after esbuild).
  candidates.push(
    path.resolve(process.cwd(), 'playwright-browsers', key),
    path.resolve(process.cwd(), 'desktop', 'playwright-browsers', key),
  );
  return [...new Set(candidates)];
}

function isStagedRegistry(candidate: string): boolean {
  return existsSync(path.join(candidate, STAGED_BROWSER_MARKER));
}

/**
 * Configure Playwright's official registry variable exactly once, before the
 * package root import in playwright-compat.
 *
 * Precedence:
 * 1. the official PLAYWRIGHT_BROWSERS_PATH supplied by the operator;
 * 2. Crew's explicit registry override;
 * 3. a platform/architecture-specific packaged or development registry.
 */
export function configurePlaywrightBrowserRegistry(): string {
  const official = process.env.PLAYWRIGHT_BROWSERS_PATH?.trim();
  if (official) return official;

  const explicit = process.env.CREW_PLAYWRIGHT_BROWSERS_PATH?.trim();
  if (explicit) {
    const resolved = path.resolve(explicit);
    process.env.PLAYWRIGHT_BROWSERS_PATH = resolved;
    return resolved;
  }

  const staged = stagedRegistryCandidates().find(isStagedRegistry);
  if (!staged) return '';
  process.env.PLAYWRIGHT_BROWSERS_PATH = staged;
  return staged;
}

export function configuredPlaywrightBrowserRegistry(): string {
  return process.env.PLAYWRIGHT_BROWSERS_PATH?.trim() ?? '';
}
