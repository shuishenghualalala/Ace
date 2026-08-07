import { createHash, createHmac, randomBytes, timingSafeEqual } from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import { resolveCrewHome } from './crew-session-file';

export const GATEWAY_INSTANCE_CHALLENGE_HEADER = 'X-Crew-Gateway-Challenge';
export const GATEWAY_INSTANCE_DIRECTORY = '.gateway-instance';
export const GATEWAY_INSTANCE_KEY_FILENAME = 'gateway-instance.key';

const PROOF_CONTEXT = Buffer.from('crew-gateway-instance-v1\0', 'ascii');
const ACCESS_TOKEN_CONTEXT = Buffer.from('crew-gateway-browser-access-v1\0', 'ascii');
const SECURITY_PROOF_CONTEXT = Buffer.from('crew-security-desktop-v1\0', 'ascii');
const HEX_32_BYTES = /^[0-9a-f]{64}$/;
const HEALTH_TIMEOUT_MS = 3_000;

export interface GatewayInstanceVerificationOptions {
  crewHome?: string;
  fetchImpl?: typeof fetch;
  challenge?: string;
  timeoutMs?: number;
}

export interface GatewayComponentState {
  status: string;
  message?: string;
}

export interface GatewayInstanceProbe {
  verified: boolean;
  components?: Record<string, GatewayComponentState>;
}

function posixUidMatches(info: fs.Stats): boolean {
  if (process.platform === 'win32' || typeof process.getuid !== 'function') return true;
  return info.uid === process.getuid();
}

function noFollowFlag(): number {
  if (process.platform === 'win32') return 0;
  return (fs.constants as unknown as Record<string, number>).O_NOFOLLOW ?? 0;
}

function assertSecureKeyDirectory(directory: string): void {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const info = fs.lstatSync(directory);
  if (info.isSymbolicLink() || !info.isDirectory()) {
    throw new Error('gateway instance key directory must be a real directory');
  }
  if (!posixUidMatches(info)) {
    throw new Error('gateway instance key directory has an unexpected owner');
  }
  if (process.platform !== 'win32' && (info.mode & 0o777) !== 0o700) {
    throw new Error('gateway instance key directory permissions must be 0700');
  }
}

function assertSecureKeyMetadata(info: fs.Stats): void {
  if (info.isSymbolicLink() || !info.isFile()) {
    throw new Error('gateway instance key must be a regular file');
  }
  if (!posixUidMatches(info)) {
    throw new Error('gateway instance key has an unexpected owner');
  }
  if (process.platform !== 'win32' && (info.mode & 0o777) !== 0o600) {
    throw new Error('gateway instance key permissions must be 0600');
  }
  if (info.size !== 64) {
    throw new Error('gateway instance key has an invalid size');
  }
}

function readSecureKey(keyFile: string): Buffer {
  const before = fs.lstatSync(keyFile);
  assertSecureKeyMetadata(before);

  const fd = fs.openSync(keyFile, fs.constants.O_RDONLY | noFollowFlag());
  try {
    const opened = fs.fstatSync(fd);
    assertSecureKeyMetadata(opened);
    if (before.dev !== opened.dev || before.ino !== opened.ino) {
      throw new Error('gateway instance key changed while opening');
    }

    const bounded = Buffer.alloc(65);
    let read = 0;
    while (read < bounded.length) {
      const count = fs.readSync(fd, bounded, read, bounded.length - read, null);
      if (count === 0) break;
      read += count;
    }
    const encoded = bounded.subarray(0, read).toString('ascii');
    if (read !== 64 || !HEX_32_BYTES.test(encoded)) {
      throw new Error('gateway instance key must be exactly 32-byte lowercase hex');
    }
    return Buffer.from(encoded, 'hex');
  } finally {
    fs.closeSync(fd);
  }
}

/** Load the persistent instance key, creating it atomically on first Desktop use. */
export function loadOrCreateGatewayInstanceKey(crewHome = resolveCrewHome()): Buffer {
  const directory = path.join(path.resolve(crewHome), GATEWAY_INSTANCE_DIRECTORY);
  const keyFile = path.join(directory, GATEWAY_INSTANCE_KEY_FILENAME);
  assertSecureKeyDirectory(directory);

  try {
    return readSecureKey(keyFile);
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code !== 'ENOENT') throw error;
  }

  const encoded = Buffer.from(randomBytes(32).toString('hex'), 'ascii');
  let fd: number | null = null;
  let created = false;
  let creationError: unknown = null;
  try {
    fd = fs.openSync(
      keyFile,
      fs.constants.O_WRONLY
        | fs.constants.O_CREAT
        | fs.constants.O_EXCL
        | noFollowFlag(),
      0o600,
    );
    created = true;
    let written = 0;
    while (written < encoded.length) {
      const count = fs.writeSync(fd, encoded, written, encoded.length - written);
      if (count <= 0) throw new Error('failed to write gateway instance key');
      written += count;
    }
    fs.fchmodSync(fd, 0o600);
    fs.fsyncSync(fd);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'EEXIST') {
      creationError = error;
    }
  } finally {
    if (fd !== null) fs.closeSync(fd);
  }
  if (creationError) {
    if (created) {
      try { fs.unlinkSync(keyFile); } catch { /* best effort cleanup */ }
    }
    throw creationError;
  }

  return readSecureKey(keyFile);
}

/** Derive a stable browser-control token without exposing the instance key. */
export function gatewayInstanceAccessToken(crewHome = resolveCrewHome()): string {
  return createHmac('sha256', loadOrCreateGatewayInstanceKey(crewHome))
    .update(ACCESS_TOKEN_CONTEXT)
    .digest('hex');
}

function healthEndpoint(baseUrl: string): URL | null {
  try {
    const parsed = new URL(baseUrl);
    if (
      parsed.protocol !== 'http:'
      || parsed.hostname !== '127.0.0.1'
      || !/^\d+$/.test(parsed.port)
      || parsed.username !== ''
      || parsed.password !== ''
      || (parsed.pathname !== '' && parsed.pathname !== '/')
      || parsed.search !== ''
      || parsed.hash !== ''
    ) {
      return null;
    }
    parsed.pathname = '/api/health';
    return parsed;
  } catch {
    return null;
  }
}

function expectedProof(key: Buffer, challenge: string): Buffer {
  return createHmac('sha256', key)
    .update(PROOF_CONTEXT)
    .update(challenge, 'ascii')
    .digest();
}

function parseComponents(raw: unknown): Record<string, GatewayComponentState> | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const parsed: Record<string, GatewayComponentState> = {};
  for (const [name, value] of Object.entries(raw)) {
    if (!value || typeof value !== 'object') continue;
    const record = value as Record<string, unknown>;
    if (typeof record.status !== 'string') continue;
    parsed[name] = {
      status: record.status.slice(0, 50),
      ...(typeof record.message === 'string' ? { message: record.message.slice(0, 500) } : {}),
    };
  }
  return Object.keys(parsed).length ? parsed : undefined;
}

/** Prove the loopback Gateway identity and return its authenticated component status. */
export async function probeGatewayInstance(
  baseUrl: string,
  options: GatewayInstanceVerificationOptions = {},
): Promise<GatewayInstanceProbe> {
  const endpoint = healthEndpoint(baseUrl);
  if (!endpoint) return { verified: false };

  const challenge = options.challenge ?? randomBytes(32).toString('hex');
  if (!HEX_32_BYTES.test(challenge)) return { verified: false };

  let key: Buffer;
  try {
    key = loadOrCreateGatewayInstanceKey(options.crewHome);
  } catch {
    return { verified: false };
  }

  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    Math.max(100, options.timeoutMs ?? HEALTH_TIMEOUT_MS),
  );
  try {
    const response = await (options.fetchImpl ?? fetch)(endpoint, {
      method: 'GET',
      headers: { [GATEWAY_INSTANCE_CHALLENGE_HEADER]: challenge },
      redirect: 'error',
      signal: controller.signal,
    });
    const contentType = response.headers.get('content-type') ?? '';
    if (!response.ok || !contentType.toLowerCase().includes('application/json')) {
      return { verified: false };
    }
    const body = await response.json().catch(() => null) as unknown;
    if (!body || typeof body !== 'object') return { verified: false };
    const record = body as Record<string, unknown>;
    if (record.ok !== true || record.service !== 'crew-gateway') return { verified: false };
    const proof = record.instance_proof;
    if (typeof proof !== 'string' || !HEX_32_BYTES.test(proof)) return { verified: false };

    const actual = Buffer.from(proof, 'hex');
    const expected = expectedProof(key, challenge);
    if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) {
      return { verified: false };
    }
    const components = parseComponents(record.components);
    return { verified: true, ...(components ? { components } : {}) };
  } catch {
    return { verified: false };
  } finally {
    clearTimeout(timeout);
  }
}

const SECURITY_PROOF_PATH_PREFIXES = ['/api/security/', '/api/mcp/cua-driver/setup'];
const CUA_SETUP_PATH = '/api/mcp/cua-driver/setup';
const CUA_CANCEL_PATH = /^\/api\/mcp\/cua-driver\/setup\/[^/]+\/cancel$/;

export type CuaSetupAuthorityAction = 'install' | 'cancel';

/**
 * Validate the exact CUA authority surface before main signs it.
 *
 * Install accepts only the two backend booleans. Cancel has no body. Status
 * reads and unknown descendants never receive authority.
 */
export function classifyCuaSetupAuthorityRequest(
  method: string,
  pathname: string,
  body: string,
): CuaSetupAuthorityAction | null {
  if (method.toUpperCase() !== 'POST') return null;
  if (pathname === CUA_SETUP_PATH) {
    let payload: unknown;
    try {
      payload = body ? JSON.parse(body) : {};
    } catch {
      return null;
    }
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    const record = payload as Record<string, unknown>;
    if (
      Object.keys(record).some((key) => !['force_reinstall', 'start_daemon'].includes(key))
      || Object.values(record).some((value) => typeof value !== 'boolean')
    ) {
      return null;
    }
    return 'install';
  }
  return CUA_CANCEL_PATH.test(pathname) && body.length === 0 ? 'cancel' : null;
}

/** Sign one security-authority REST request; renderer code never receives this key or proof.
 *
 * The proof binds ``timestamp`` + a fresh one-time ``nonce`` + method + pathname +
 * body hash. The gateway consumes the nonce after a successful verify, so the same
 * proof cannot be replayed within its TTL even for an identical request (H-19).
 * Pathname must be a security-authority route or the CUA driver setup route (the
 * latter runs remote installer scripts on the host and is treated as authority).
 */
export function createDesktopSecurityProof(
  method: string,
  pathname: string,
  body: string,
  options: { crewHome?: string; nowSeconds?: number; nonce?: string } = {},
): string {
  if (!SECURITY_PROOF_PATH_PREFIXES.some((p) => pathname.startsWith(p))) {
    throw new Error('invalid security proof path');
  }
  const timestamp = Math.floor(options.nowSeconds ?? Date.now() / 1000);
  const nonce = options.nonce ?? randomBytes(16).toString('hex');
  const plainBodyHash = createHash('sha256').update(body, 'utf8').digest('hex');
  const message = Buffer.concat([
    SECURITY_PROOF_CONTEXT,
    Buffer.from(`${timestamp}\n${nonce}\n${method.toUpperCase()}\n${pathname}\n${plainBodyHash}`, 'utf8'),
  ]);
  const key = loadOrCreateGatewayInstanceKey(options.crewHome);
  const signature = createHmac('sha256', key).update(message).digest('hex');
  return `${timestamp}:${nonce}:${signature}`;
}

/**
 * Prove that a loopback HTTP listener is this user's Crew Gateway before any
 * login JWT or employee identity headers are sent to it.
 */
export async function verifyGatewayInstance(
  baseUrl: string,
  options: GatewayInstanceVerificationOptions = {},
): Promise<boolean> {
  return (await probeGatewayInstance(baseUrl, options)).verified;
}
