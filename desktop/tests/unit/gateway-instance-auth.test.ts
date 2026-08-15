import { createHash, createHmac } from 'crypto';
import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  GATEWAY_INSTANCE_CHALLENGE_HEADER,
  GATEWAY_INSTANCE_DIRECTORY,
  GATEWAY_INSTANCE_KEY_FILENAME,
  createDesktopSecurityProof,
  loadOrCreateGatewayInstanceKey,
  probeGatewayInstance,
  verifyGatewayInstance,
} from '../../src/main/gateway-instance-auth';

vi.setConfig({ testTimeout: process.platform === 'win32' ? 30_000 : 5_000 });

const tempRoots: string[] = [];
const PROOF_CONTEXT = Buffer.from('crew-gateway-instance-v1\0', 'ascii');
const SECURITY_PROOF_CONTEXT = Buffer.from('crew-security-desktop-v1\0', 'ascii');

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

function powershellQuote(value: string): string {
  return `'${value.replaceAll("'", "''")}'`;
}

function windowsAclSnapshot(target: string): {
  protected: boolean;
  owner: string;
  current: string;
  rules: Array<{
    identity: string;
    rights: number;
    type: number;
    inherited: boolean;
    inheritance: number;
    propagation: number;
  }>;
} {
  const powershell = path.join(
    process.env['SystemRoot'] || 'C:\\Windows',
    'System32',
    'WindowsPowerShell',
    'v1.0',
    'powershell.exe',
  );
  const script = [
    "$ErrorActionPreference = 'Stop'",
    `$target = ${powershellQuote(target)}`,
    '$acl = if ([System.IO.Directory]::Exists($target)) { [System.IO.DirectoryInfo]::new($target).GetAccessControl() } else { [System.IO.FileInfo]::new($target).GetAccessControl() }',
    '$rules = @($acl.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]) | ForEach-Object { [pscustomobject]@{ identity = $_.IdentityReference.Value; rights = [int]$_.FileSystemRights; type = [int]$_.AccessControlType; inherited = $_.IsInherited; inheritance = [int]$_.InheritanceFlags; propagation = [int]$_.PropagationFlags } })',
    '[pscustomobject]@{ protected = $acl.AreAccessRulesProtected; owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value; current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; rules = $rules } | ConvertTo-Json -Depth 4 -Compress',
  ].join('; ');
  const result = spawnSync(powershell, [
    '-NoProfile',
    '-NonInteractive',
    '-EncodedCommand',
    Buffer.from(script, 'utf16le').toString('base64'),
  ], {
    encoding: 'utf8',
    windowsHide: true,
  });
  if (result.error || result.status !== 0) {
    throw result.error ?? new Error(String(result.stderr || 'failed to inspect Windows ACL'));
  }
  return JSON.parse(String(result.stdout)) as ReturnType<typeof windowsAclSnapshot>;
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

  it('creates Windows key objects with protected host-only ACLs', () => {
    if (process.platform !== 'win32') return;
    const crewHome = tempCrewHome();
    loadOrCreateGatewayInstanceKey(crewHome);
    const directory = path.join(crewHome, GATEWAY_INSTANCE_DIRECTORY);
    const keyFile = path.join(directory, GATEWAY_INSTANCE_KEY_FILENAME);
    const expectedSids = [
      windowsAclSnapshot(keyFile).current,
      'S-1-5-18',
      'S-1-5-32-544',
    ].sort();

    for (const [target, expectedInheritance] of [[directory, 3], [keyFile, 0]] as const) {
      const snapshot = windowsAclSnapshot(target);
      expect(snapshot.protected).toBe(true);
      expect(snapshot.owner).toBe(snapshot.current);
      expect(snapshot.rules).toHaveLength(3);
      expect(snapshot.rules.map((rule) => rule.identity).sort()).toEqual(expectedSids);
      for (const rule of snapshot.rules) {
        expect(rule.rights).toBe(0x001f01ff);
        expect(rule.type).toBe(0);
        expect(rule.inherited).toBe(false);
        expect(rule.inheritance).toBe(expectedInheritance);
        expect(rule.propagation).toBe(0);
      }
    }

    const tampered = spawnSync('icacls.exe', [keyFile, '/grant', '*S-1-1-0:R'], {
      encoding: 'utf8',
      windowsHide: true,
    });
    expect(tampered.status).toBe(0);
    expect(() => loadOrCreateGatewayInstanceKey(crewHome)).toThrow(/ACL validation failed/);
  }, 30_000);

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

describe('desktop security proof', () => {
  it('uses a per-launch key instead of the readable persistent installation key', () => {
    const crewHome = tempCrewHome();
    const persistentKey = loadOrCreateGatewayInstanceKey(crewHome);
    const instanceKey = Buffer.from('42'.repeat(32), 'hex');
    const nowSeconds = 1_800_000_000;
    const nonce = '11'.repeat(16);
    const pathname = '/api/sessions';
    const bodyHash = createHash('sha256').update('').digest('hex');
    const message = Buffer.concat([
      SECURITY_PROOF_CONTEXT,
      Buffer.from(`${nowSeconds}\n${nonce}\nGET\n${pathname}\n${bodyHash}`, 'utf8'),
    ]);
    const proof = createDesktopSecurityProof('GET', pathname, '', {
      crewHome,
      instanceKey,
      nowSeconds,
      nonce,
    });

    expect(proof).toBe(
      `${nowSeconds}:${nonce}:${createHmac('sha256', instanceKey).update(message).digest('hex')}`,
    );
    expect(proof).not.toBe(
      `${nowSeconds}:${nonce}:${createHmac('sha256', persistentKey).update(message).digest('hex')}`,
    );
  }, 30_000);

  it('uses only the explicitly selected Gateway home', () => {
    const accountHome = tempCrewHome();
    const activeGatewayHome = tempCrewHome();
    const method = 'GET';
    const pathname = '/api/security/capabilities';
    const body = '';
    const nowSeconds = 1_800_000_000;
    const nonce = '12'.repeat(16);
    const proof = createDesktopSecurityProof(method, pathname, body, {
      crewHome: activeGatewayHome,
      nowSeconds,
      nonce,
    });
    const activeKey = loadOrCreateGatewayInstanceKey(activeGatewayHome);
    const accountKey = loadOrCreateGatewayInstanceKey(accountHome);
    const bodyHash = createHash('sha256').update(body, 'utf8').digest('hex');
    const message = Buffer.concat([
      SECURITY_PROOF_CONTEXT,
      Buffer.from(`${nowSeconds}\n${nonce}\n${method}\n${pathname}\n${bodyHash}`, 'utf8'),
    ]);
    const expected = createHmac('sha256', activeKey).update(message).digest('hex');
    const wrongHomeSignature = createHmac('sha256', accountKey).update(message).digest('hex');

    expect(proof).toBe(`${nowSeconds}:${nonce}:${expected}`);
    expect(proof).not.toBe(`${nowSeconds}:${nonce}:${wrongHomeSignature}`);
  }, 30_000);

  it('rejects a relative Gateway home', () => {
    expect(() => createDesktopSecurityProof('GET', '/api/security/capabilities', '', {
      crewHome: 'relative-gateway-home',
    })).toThrow(/must be absolute/);
  });

  it('signs ordinary REST, WebSocket, and exact multipart request bytes', () => {
    const crewHome = tempCrewHome();
    const options = {
      crewHome,
      nowSeconds: 1_800_000_000,
      nonce: '34'.repeat(16),
    };

    expect(createDesktopSecurityProof('GET', '/api/sessions', '', options))
      .toMatch(/^1800000000:[0-9a-f]{32}:[0-9a-f]{64}$/);
    expect(createDesktopSecurityProof('GET', '/ws', '', {
      ...options,
      nonce: '35'.repeat(16),
    })).toMatch(/^1800000000:[0-9a-f]{32}:[0-9a-f]{64}$/);
    const multipartBody = Buffer.from('--boundary\r\ncontent\r\n--boundary--\r\n');
    const multipartProof = createDesktopSecurityProof('POST', '/api/wiki/upload', multipartBody, {
      ...options,
      nonce: '36'.repeat(16),
    });
    expect(multipartProof).toMatch(/^1800000000:[0-9a-f]{32}:[0-9a-f]{64}$/);
    expect(createDesktopSecurityProof(
      'POST',
      '/api/wiki/upload',
      Buffer.from('--boundary\r\ntampered\r\n--boundary--\r\n'),
      { ...options, nonce: '36'.repeat(16) },
    )).not.toBe(multipartProof);
    expect(() => createDesktopSecurityProof(
      'POST',
      '/api/wiki/upload',
      null as never,
      options,
    )).toThrow();
    expect(() => createDesktopSecurityProof('GET', '/not-gateway', '', options))
      .toThrow(/invalid security proof path/);
  }, 30_000);
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
      status: 'verified',
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

  it('classifies network failures as unreachable instead of identity mismatches', async () => {
    const crewHome = tempCrewHome();
    const unavailable = vi.fn(async () => {
      throw new TypeError('fetch failed');
    }) as unknown as typeof fetch;

    const result = await probeGatewayInstance('http://127.0.0.1:8000', {
      crewHome,
      fetchImpl: unavailable,
      challenge: 'ab'.repeat(32),
    });

    expect(result).toEqual({ status: 'unreachable', verified: false });
  });

  it('classifies a reachable service with the wrong proof as untrusted', async () => {
    const crewHome = tempCrewHome();
    const wrongProof = vi.fn(async () => jsonResponse({
      ok: true,
      service: 'crew-gateway',
      instance_proof: '00'.repeat(32),
    })) as unknown as typeof fetch;

    const result = await probeGatewayInstance('http://127.0.0.1:8000', {
      crewHome,
      fetchImpl: wrongProof,
      challenge: 'ab'.repeat(32),
    });

    expect(result).toEqual({ status: 'untrusted', verified: false });
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
