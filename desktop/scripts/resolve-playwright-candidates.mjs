/**
 * Resolve forward-compatibility versions for CI without changing package.json.
 *
 * With an argument, only that exact published version is returned. Without an
 * argument, both the current stable (`latest`) and prerelease (`next`) tags are
 * resolved. The result is a compact JSON array suitable for fromJSON().
 */

import { execFileSync } from 'node:child_process';

const exactVersion = process.argv[2]?.trim() ?? '';
const exactSemver =
  /^\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$/;

if (exactVersion && !exactSemver.test(exactVersion)) {
  throw new Error(`Playwright candidate must be an exact semver: ${exactVersion}`);
}

function npmVersion(packageName, versionOrTag) {
  const raw = execFileSync(
    'npm',
    ['view', `${packageName}@${versionOrTag}`, 'version', '--json'],
    { encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] },
  );
  const parsed = JSON.parse(raw);
  if (typeof parsed !== 'string' || !exactSemver.test(parsed)) {
    throw new Error(
      `npm returned an invalid version for ${packageName}@${versionOrTag}: ${raw.trim()}`,
    );
  }
  return parsed;
}

const requests = exactVersion ? [exactVersion] : ['latest', 'next'];
const versions = [];
for (const request of requests) {
  const core = npmVersion('playwright-core', request);
  const test = npmVersion('@playwright/test', request);
  if (core !== test) {
    throw new Error(
      `Playwright packages are skewed for ${request}: playwright-core=${core},`
      + ` @playwright/test=${test}`,
    );
  }
  if (!versions.includes(core)) versions.push(core);
}

process.stdout.write(JSON.stringify(versions));
