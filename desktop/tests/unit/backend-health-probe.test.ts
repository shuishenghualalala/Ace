import { createHmac } from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  GATEWAY_INSTANCE_CHALLENGE_HEADER,
  loadOrCreateGatewayInstanceKey,
} from '../../src/main/gateway-instance-auth';
import { healthPortUrlFor, probeBackendHealth } from '../../src/main/backend-health-probe';

const tempRoots: string[] = [];
const PROOF_CONTEXT = Buffer.from('crew-gateway-instance-v1\0', 'ascii');

function tempCrewHome(): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'crew-backend-health-probe-'));
  tempRoots.push(root);
  return root;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function connRefused(): TypeError {
  return Object.assign(new TypeError('fetch failed'), { code: 'ECONNREFUSED' });
}

/** 按端口路由的假 fetch：proof 用真实实例密钥计算，与 desktop 生产路径一致。 */
function routingFetch(
  key: Buffer,
  routes: (url: URL, challenge: string) => Promise<Response> | Response,
) {
  return vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input));
    const headers = (init?.headers ?? {}) as Record<string, string>;
    return routes(url, headers[GATEWAY_INSTANCE_CHALLENGE_HEADER]!);
  }) as unknown as typeof fetch;
}

function proofBody(key: Buffer, challenge: string, extra: Record<string, unknown> = {}) {
  const proof = createHmac('sha256', key)
    .update(PROOF_CONTEXT)
    .update(challenge, 'ascii')
    .digest('hex');
  return { ok: true, service: 'crew-gateway', instance_proof: proof, ...extra };
}

afterEach(() => {
  for (const root of tempRoots.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

describe('healthPortUrlFor', () => {
  it('maps the main port to main port + 1', () => {
    expect(healthPortUrlFor('http://127.0.0.1:8000')).toBe('http://127.0.0.1:8001/');
    expect(healthPortUrlFor('http://127.0.0.1:8009/api/health')).toBe('http://127.0.0.1:8010/');
  });

  it('returns null when the port cannot be derived or the URL is invalid', () => {
    expect(healthPortUrlFor('http://127.0.0.1')).toBeNull(); // 默认端口无法推导 +1
    expect(healthPortUrlFor('not a url')).toBeNull();
  });
});

describe('probeBackendHealth', () => {
  it('prefers the health thread port and surfaces loop_lag_ms', async () => {
    const crewHome = tempCrewHome();
    const key = loadOrCreateGatewayInstanceKey(crewHome);
    const fetchImpl = routingFetch(key, (url, challenge) => {
      expect(url.port).toBe('8001');
      return jsonResponse(proofBody(key, challenge, { loop_lag_ms: 42 }));
    });

    const result = await probeBackendHealth('http://127.0.0.1:8000', { crewHome, fetchImpl });

    expect(result.verified).toBe(true);
    expect(result.loopLagMs).toBe(42);
    expect((fetchImpl as unknown as { mock: { calls: unknown[] } }).mock.calls).toHaveLength(1);
  });

  it('falls back to the main port when the health port is unreachable (old gateway)', async () => {
    const crewHome = tempCrewHome();
    const key = loadOrCreateGatewayInstanceKey(crewHome);
    const calls: string[] = [];
    const fetchImpl = routingFetch(key, async (url, challenge) => {
      calls.push(url.toString());
      if (url.port === '8001') throw connRefused();
      return jsonResponse(proofBody(key, challenge));
    });

    const result = await probeBackendHealth('http://127.0.0.1:8000', { crewHome, fetchImpl });

    expect(result.verified).toBe(true);
    expect(result.loopLagMs).toBeUndefined(); // 主端口路径不带 loop_lag_ms
    expect(calls).toEqual([
      'http://127.0.0.1:8001/api/health',
      'http://127.0.0.1:8000/api/health',
    ]);
  });

  it('falls back when the health port answers but the proof does not verify', async () => {
    const crewHome = tempCrewHome();
    const key = loadOrCreateGatewayInstanceKey(crewHome);
    const fetchImpl = routingFetch(key, (url, challenge) => {
      if (url.port === '8001') {
        return jsonResponse(proofBody(Buffer.from('22'.repeat(32), 'hex'), challenge));
      }
      return jsonResponse(proofBody(key, challenge));
    });

    const result = await probeBackendHealth('http://127.0.0.1:8000', { crewHome, fetchImpl });

    expect(result.verified).toBe(true);
  });

  it('reports the failure when both the health port and the main port fail', async () => {
    const crewHome = tempCrewHome();
    const key = loadOrCreateGatewayInstanceKey(crewHome);
    const fetchImpl = routingFetch(key, async () => {
      throw connRefused();
    });

    const result = await probeBackendHealth('http://127.0.0.1:8000', { crewHome, fetchImpl });

    expect(result.verified).toBe(false);
    expect(result.failureKind).toBe('unreachable');
  });
});
