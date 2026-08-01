import { createHmac } from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  GATEWAY_INSTANCE_CHALLENGE_HEADER,
  GATEWAY_INSTANCE_DIRECTORY,
  GATEWAY_INSTANCE_KEY_FILENAME,
  loadOrCreateGatewayInstanceKey,
  probeGatewayInstance,
  verifyGatewayInstance,
} from '../../src/main/gateway-instance-auth';

const tempRoots: string[] = [];
const PROOF_CONTEXT = Buffer.from('crew-gateway-instance-v1\0', 'ascii');

function tempCrewHome(): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'crew-gateway-instance-'));
  tempRoots.push(root);
  return root;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  for (const root of tempRoots.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

describe('gateway instance key', () => {
  it('creates a persistent 32-byte key with private metadata', () => {
    const crewHome = tempCrewHome();
    const first = loadOrCreateGatewayInstanceKey(crewHome);
    const second = loadOrCreateGatewayInstanceKey(crewHome);
    const directory = path.join(crewHome, GATEWAY_INSTANCE_DIRECTORY);
    const keyFile = path.join(directory, GATEWAY_INSTANCE_KEY_FILENAME);

    expect(first).toHaveLength(32);
    expect(second.equals(first)).toBe(true);
    expect(fs.readFileSync(keyFile, 'ascii')).toMatch(/^[0-9a-f]{64}$/);
    if (process.platform !== 'win32') {
      expect(fs.statSync(directory).mode & 0o777).toBe(0o700);
      expect(fs.statSync(keyFile).mode & 0o777).toBe(0o600);
    }
  });

  it('fails closed for symlinks and wide POSIX permissions', () => {
    if (process.platform === 'win32') return;
    const crewHome = tempCrewHome();
    const directory = path.join(crewHome, GATEWAY_INSTANCE_DIRECTORY);
    const keyFile = path.join(directory, GATEWAY_INSTANCE_KEY_FILENAME);
    fs.mkdirSync(directory, { mode: 0o700 });
    const target = path.join(crewHome, 'attacker-key');
    fs.writeFileSync(target, '11'.repeat(32), { mode: 0o600 });
    fs.symlinkSync(target, keyFile);

    expect(() => loadOrCreateGatewayInstanceKey(crewHome)).toThrow(/regular file/);

    fs.unlinkSync(keyFile);
    fs.writeFileSync(keyFile, '11'.repeat(32), { mode: 0o644 });
    expect(() => loadOrCreateGatewayInstanceKey(crewHome)).toThrow(/0600/);

    fs.chmodSync(keyFile, 0o600);
    fs.chmodSync(directory, 0o755);
    expect(() => loadOrCreateGatewayInstanceKey(crewHome)).toThrow(/0700/);
  });
});

describe('verifyGatewayInstance', () => {
  it('returns verified component status from the authenticated health response', async () => {
    const crewHome = tempCrewHome();
    const key = loadOrCreateGatewayInstanceKey(crewHome);
    const fakeFetch = vi.fn(async (_input: URL | RequestInfo, init?: RequestInit) => {
      const challenge = new Headers(init?.headers).get(GATEWAY_INSTANCE_CHALLENGE_HEADER) ?? '';
      const proof = createHmac('sha256', key)
        .update(PROOF_CONTEXT)
        .update(challenge, 'ascii')
        .digest('hex');
      return jsonResponse({
        ok: true,
        service: 'crew-gateway',
        instance_proof: proof,
        components: {
          cron: { status: 'failed', message: '定时任务启动失败，请查看 Gateway 日志' },
        },
      });
    }) as unknown as typeof fetch;

    const result = await probeGatewayInstance('http://127.0.0.1:8000', {
      crewHome,
      fetchImpl: fakeFetch,
      challenge: 'ab'.repeat(32),
    });

    expect(result).toEqual({
      verified: true,
      components: {
        cron: { status: 'failed', message: '定时任务启动失败，请查看 Gateway 日志' },
      },
    });
  });

  it('accepts a valid domain-separated proof without sending login identity', async () => {
    const crewHome = tempCrewHome();
    const key = loadOrCreateGatewayInstanceKey(crewHome);
    let observedHeaders: Headers | null = null;
    const fakeFetch = vi.fn(async (_input: URL | RequestInfo, init?: RequestInit) => {
      observedHeaders = new Headers(init?.headers);
      const challenge = observedHeaders.get(GATEWAY_INSTANCE_CHALLENGE_HEADER) ?? '';
      const proof = createHmac('sha256', key)
        .update(PROOF_CONTEXT)
        .update(challenge, 'ascii')
        .digest('hex');
      return jsonResponse({ ok: true, service: 'crew-gateway', instance_proof: proof });
    }) as unknown as typeof fetch;

    const verified = await verifyGatewayInstance('http://127.0.0.1:8000', {
      crewHome,
      fetchImpl: fakeFetch,
      challenge: 'ab'.repeat(32),
    });

    expect(verified).toBe(true);
    expect(fakeFetch).toHaveBeenCalledOnce();
    expect(observedHeaders?.get(GATEWAY_INSTANCE_CHALLENGE_HEADER)).toBe('ab'.repeat(32));
    expect(observedHeaders?.has('authorization')).toBe(false);
  });

  it('rejects a fake readiness response and a proof replayed for another challenge', async () => {
    const crewHome = tempCrewHome();
    const key = loadOrCreateGatewayInstanceKey(crewHome);
    const oldChallenge = '12'.repeat(32);
    const replayedProof = createHmac('sha256', key)
      .update(PROOF_CONTEXT)
      .update(oldChallenge, 'ascii')
      .digest('hex');
    const readinessOnly = vi.fn(async () => jsonResponse({ ok: true })) as unknown as typeof fetch;
    const replay = vi.fn(async () => jsonResponse({
      ok: true,
      service: 'crew-gateway',
      instance_proof: replayedProof,
    })) as unknown as typeof fetch;

    await expect(verifyGatewayInstance('http://127.0.0.1:8000', {
      crewHome,
      fetchImpl: readinessOnly,
      challenge: oldChallenge,
    })).resolves.toBe(false);
    await expect(verifyGatewayInstance('http://127.0.0.1:8000', {
      crewHome,
      fetchImpl: replay,
      challenge: '34'.repeat(32),
    })).resolves.toBe(false);
  });

  it('refuses non-loopback and ambiguous base URLs before fetching', async () => {
    const crewHome = tempCrewHome();
    const fakeFetch = vi.fn(async () => jsonResponse({ ok: true })) as unknown as typeof fetch;

    for (const url of [
      'https://127.0.0.1:8000',
      'http://localhost:8000',
      'http://127.0.0.1:8000/other',
      'http://user@127.0.0.1:8000',
    ]) {
      await expect(verifyGatewayInstance(url, { crewHome, fetchImpl: fakeFetch })).resolves.toBe(false);
    }
    expect(fakeFetch).not.toHaveBeenCalled();
  });
});
