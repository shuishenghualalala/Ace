import { spawnSync } from 'node:child_process';
import { generateKeyPairSync } from 'node:crypto';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';

const desktopRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const buildConfig = join(desktopRoot, 'esbuild.config.mjs');
const preflightDirs: string[] = [];

function runBuildConfig(
  args: string[],
  env: Record<string, string | undefined> = {},
  cwd = desktopRoot,
) {
  const childEnv = { ...process.env };
  for (const [key, value] of Object.entries(env)) {
    if (value === undefined) delete childEnv[key];
    else childEnv[key] = value;
  }
  return spawnSync(process.execPath, [buildConfig, ...args], {
    cwd,
    env: childEnv,
    encoding: 'utf8',
  });
}

function output(result: ReturnType<typeof runBuildConfig>): string {
  return `${result.stdout}\n${result.stderr}`;
}

afterEach(() => {
  for (const directory of preflightDirs.splice(0)) rmSync(directory, { recursive: true, force: true });
});

describe('desktop production release build contract', () => {
  it('reports every missing release input before building', () => {
    const result = runBuildConfig([], {
      ACE_DOWNLOAD_BASE_URL: undefined,
      ACE_UPDATE_PUBLIC_KEY: undefined,
    });

    expect(result.status).not.toBe(0);
    expect(output(result)).toContain('ACE_DOWNLOAD_BASE_URL');
    expect(output(result)).toContain('ACE_UPDATE_PUBLIC_KEY');
    expect(output(result)).toContain('no artifact was built');
  });

  it('keeps release preflight strict even if --dev is also supplied', () => {
    const result = runBuildConfig(['--preflight', '--dev'], {
      ACE_DOWNLOAD_BASE_URL: undefined,
      ACE_UPDATE_PUBLIC_KEY: undefined,
    });

    expect(result.status).not.toBe(0);
    expect(output(result)).toContain('ACE_DOWNLOAD_BASE_URL');
    expect(output(result)).toContain('ACE_UPDATE_PUBLIC_KEY');
  });

  it('rejects unsafe URL and non-Ed25519 key in preflight', () => {
    const result = runBuildConfig(['--preflight'], {
      ACE_DOWNLOAD_BASE_URL: 'http://updates.example.test/releases',
      ACE_UPDATE_PUBLIC_KEY: 'not-a-public-key',
    });

    expect(result.status).not.toBe(0);
    expect(output(result)).toContain('ACE_DOWNLOAD_BASE_URL must use HTTPS');
    expect(output(result)).toContain('ACE_UPDATE_PUBLIC_KEY must be an Ed25519 SPKI public key');
  });

  it('accepts a generated public-key fixture without touching the build output', () => {
    const { publicKey } = generateKeyPairSync('ed25519');
    const publicKeyDer = publicKey.export({ format: 'der', type: 'spki' }).toString('base64');
    const emptyCwd = mkdtempSync(join(tmpdir(), 'ace-release-preflight-'));
    preflightDirs.push(emptyCwd);

    const result = runBuildConfig(['--preflight'], {
      ACE_DOWNLOAD_BASE_URL: 'https://updates.example.test/releases/',
      ACE_UPDATE_PUBLIC_KEY: publicKeyDer,
    }, emptyCwd);

    expect(result.status).toBe(0);
    expect(output(result)).toContain('no artifact built');
  });
});
