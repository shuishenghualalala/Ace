import { createHash, createHmac, randomBytes, timingSafeEqual } from 'crypto';
import { spawnSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { resolveCrewHome } from './crew-session-file';
import { hardenedChildProcessOptions } from './process-environment';

export const GATEWAY_INSTANCE_CHALLENGE_HEADER = 'X-Crew-Gateway-Challenge';
export const GATEWAY_INSTANCE_DIRECTORY = '.gateway-instance';
export const GATEWAY_INSTANCE_KEY_FILENAME = 'gateway-instance.key';
export const GATEWAY_INSTANCE_AUTH_HEADER = 'X-Crew-Security-Proof';
export const DESKTOP_REQUEST_ORIGIN = 'ace-desktop://main';

const PROOF_CONTEXT = Buffer.from('crew-gateway-instance-v1\0', 'ascii');
const ACCESS_TOKEN_CONTEXT = Buffer.from('crew-gateway-browser-access-v1\0', 'ascii');
const SECURITY_PROOF_CONTEXT = Buffer.from('crew-security-desktop-v1\0', 'ascii');
const HEX_32_BYTES = /^[0-9a-f]{64}$/;
const HEALTH_TIMEOUT_MS = 3_000;

function quotePowerShell(value: string): string {
  return `'${value.replaceAll("'", "''")}'`;
}

type WindowsKeyCacheEntry = {
  directoryFingerprint: string;
  fileFingerprint: string;
  key: Buffer;
};

const windowsKeyCache = new Map<string, WindowsKeyCacheEntry>();

function windowsObjectFingerprint(target: string, directory: boolean): string | null {
  try {
    const info = fs.lstatSync(target, { bigint: true });
    if (info.isSymbolicLink() || (directory ? !info.isDirectory() : !info.isFile())) return null;
    return [
      info.dev,
      info.ino,
      info.size,
      info.mtimeNs,
      info.ctimeNs,
    ].join(':');
  } catch {
    return null;
  }
}

function cachedWindowsKey(directory: string, keyFile: string): Buffer | null {
  if (process.platform !== 'win32') return null;
  const cached = windowsKeyCache.get(keyFile);
  if (
    !cached
    || cached.directoryFingerprint !== windowsObjectFingerprint(directory, true)
    || cached.fileFingerprint !== windowsObjectFingerprint(keyFile, false)
  ) {
    windowsKeyCache.delete(keyFile);
    return null;
  }
  return Buffer.from(cached.key);
}

function cacheWindowsKey(directory: string, keyFile: string, key: Buffer): void {
  if (process.platform !== 'win32') return;
  const directoryFingerprint = windowsObjectFingerprint(directory, true);
  const fileFingerprint = windowsObjectFingerprint(keyFile, false);
  if (!directoryFingerprint || !fileFingerprint) return;
  if (windowsKeyCache.size >= 16 && !windowsKeyCache.has(keyFile)) {
    const oldest = windowsKeyCache.keys().next().value as string | undefined;
    if (oldest) windowsKeyCache.delete(oldest);
  }
  windowsKeyCache.set(keyFile, {
    directoryFingerprint,
    fileFingerprint,
    key: Buffer.from(key),
  });
}

function validateOrCreateWindowsKey(
  directory: string,
  keyFile: string,
  encoded: Buffer | null,
): void {
  if (process.platform !== 'win32') return;
  if (!path.isAbsolute(directory) || !path.isAbsolute(keyFile)) {
    throw new Error('Windows key ACL targets must be absolute');
  }
  const powershell = path.join(
    'C:\\Windows',
    'System32',
    'WindowsPowerShell',
    'v1.0',
    'powershell.exe',
  );
  const script = [
    "$ErrorActionPreference = 'Stop'",
    `$directory = ${quotePowerShell(directory)}`,
    `$keyFile = ${quotePowerShell(keyFile)}`,
    '$encoded = [Console]::In.ReadToEnd()',
    '$create = $encoded.Length -gt 0',
    '$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User',
    "$system = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')",
    "$admins = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')",
    '$expected = @($user.Value, $system.Value, $admins.Value)',
    'function New-HostSecurity([bool]$container) { $security = if ($container) { New-Object System.Security.AccessControl.DirectorySecurity } else { New-Object System.Security.AccessControl.FileSecurity }; $security.SetOwner($user); $security.SetAccessRuleProtection($true, $false); $inheritance = if ($container) { [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit } else { [System.Security.AccessControl.InheritanceFlags]::None }; foreach ($sid in @($user, $system, $admins)) { $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, [System.Security.AccessControl.FileSystemRights]::FullControl, $inheritance, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Allow); [void]$security.AddAccessRule($rule) }; return $security }',
    'function Assert-HostAcl([string]$target, [bool]$container) { $item = Get-Item -LiteralPath $target -Force; if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or $item.PSIsContainer -ne $container) { throw "invalid key object type" }; $actual = Get-Acl -LiteralPath $target; if (-not $actual.AreAccessRulesProtected -or $actual.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $user.Value) { throw "invalid key owner or DACL protection" }; $rules = @($actual.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier])); if ($rules.Count -ne 3) { throw "unexpected ACE count" }; $inheritance = if ($container) { [int]([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit) } else { 0 }; $seen = @{}; foreach ($rule in $rules) { if ($rule.IsInherited -or [int]$rule.AccessControlType -ne 0 -or [int]$rule.FileSystemRights -ne 0x001f01ff -or [int]$rule.InheritanceFlags -ne $inheritance -or [int]$rule.PropagationFlags -ne 0 -or $expected -notcontains $rule.IdentityReference.Value -or $seen.ContainsKey($rule.IdentityReference.Value)) { throw "unexpected DACL ACE" }; $seen[$rule.IdentityReference.Value] = $true }; if ($seen.Count -ne 3) { throw "missing DACL ACE" } }',
    '$createdDirectory = $false',
    '$createdFile = $false',
    'try { if (-not [System.IO.Directory]::Exists($directory)) { if (-not $create) { throw "key directory missing" }; $parent = New-Object System.IO.DirectoryInfo([System.IO.Path]::GetDirectoryName($directory)); [void]$parent.CreateSubdirectory([System.IO.Path]::GetFileName($directory), (New-HostSecurity $true)); $createdDirectory = $true }; Assert-HostAcl $directory $true; if (-not [System.IO.File]::Exists($keyFile)) { if (-not $create) { throw "key file missing" }; $stream = $null; try { $stream = New-Object System.IO.FileStream($keyFile, [System.IO.FileMode]::CreateNew, [System.Security.AccessControl.FileSystemRights]::Write, [System.IO.FileShare]::None, 4096, [System.IO.FileOptions]::WriteThrough, (New-HostSecurity $false)); $createdFile = $true; $bytes = [System.Text.Encoding]::ASCII.GetBytes($encoded); $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } catch [System.IO.IOException] { if ($createdFile -or -not [System.IO.File]::Exists($keyFile)) { throw } } finally { if ($null -ne $stream) { $stream.Dispose() } } }; Assert-HostAcl $keyFile $false } catch { if ($createdFile) { [System.IO.File]::Delete($keyFile) }; if ($createdDirectory) { try { [System.IO.Directory]::Delete($directory) } catch {} }; throw }',
  ].join('; ');
  const result = spawnSync(
    powershell,
    [
      '-NoProfile',
      '-NonInteractive',
      '-EncodedCommand',
      Buffer.from(script, 'utf16le').toString('base64'),
    ],
    hardenedChildProcessOptions(
      {
        cwd: path.dirname(powershell),
        encoding: 'utf8',
        windowsHide: true,
        timeout: 15_000,
        input: encoded?.toString('ascii') ?? '',
        stdio: ['pipe', 'pipe', 'pipe'],
      },
      {
        PSModulePath: path.join(path.dirname(powershell), 'Modules'),
      },
    ),
  );
  if (result.error || result.status !== 0) {
    throw new Error(
      `Windows Gateway instance key ACL validation failed: ${
        result.error?.message || `exit ${result.status}`
      }`,
    );
  }
}

export interface GatewayInstanceVerificationOptions {
  crewHome?: string;
  instanceKey?: Buffer;
  fetchImpl?: typeof fetch;
  challenge?: string;
  timeoutMs?: number;
}

function gatewayInstanceKey(crewHome: string | undefined, instanceKey?: Buffer): Buffer {
  if (instanceKey !== undefined) {
    if (instanceKey.length !== 32) throw new Error('Gateway instance key must be 32 bytes');
    return Buffer.from(instanceKey);
  }
  return loadOrCreateGatewayInstanceKey(crewHome);
}

export interface GatewayComponentState {
  status: string;
  message?: string;
}

export interface GatewayInstanceProbe {
  status: 'verified' | 'unreachable' | 'untrusted';
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
  fs.mkdirSync(path.dirname(directory), { recursive: true });
  if (process.platform !== 'win32') {
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  } else if (!fs.existsSync(directory)) {
    return;
  }
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
  const cached = cachedWindowsKey(directory, keyFile);
  if (cached) return cached;
  assertSecureKeyDirectory(directory);

  if (process.platform === 'win32') {
    const encoded = fs.existsSync(keyFile)
      ? null
      : Buffer.from(randomBytes(32).toString('hex'), 'ascii');
    validateOrCreateWindowsKey(directory, keyFile, encoded);
    assertSecureKeyDirectory(directory);
    const key = readSecureKey(keyFile);
    cacheWindowsKey(directory, keyFile, key);
    return key;
  }

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
export function gatewayInstanceAccessToken(
  crewHome = resolveCrewHome(),
  instanceKey?: Buffer,
): string {
  return createHmac('sha256', gatewayInstanceKey(crewHome, instanceKey))
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
  if (!endpoint) return { status: 'untrusted', verified: false };

  const challenge = options.challenge ?? randomBytes(32).toString('hex');
  if (!HEX_32_BYTES.test(challenge)) return { status: 'untrusted', verified: false };

  let key: Buffer;
  try {
    key = gatewayInstanceKey(options.crewHome, options.instanceKey);
  } catch {
    return { status: 'untrusted', verified: false };
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
      return { status: 'untrusted', verified: false };
    }
    const body = await response.json().catch(() => null) as unknown;
    if (!body || typeof body !== 'object') return { status: 'untrusted', verified: false };
    const record = body as Record<string, unknown>;
    if (record.ok !== true || record.service !== 'crew-gateway') {
      return { status: 'untrusted', verified: false };
    }
    const proof = record.instance_proof;
    if (typeof proof !== 'string' || !HEX_32_BYTES.test(proof)) {
      return { status: 'untrusted', verified: false };
    }

    const actual = Buffer.from(proof, 'hex');
    const expected = expectedProof(key, challenge);
    if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) {
      return { status: 'untrusted', verified: false };
    }
    const components = parseComponents(record.components);
    return { status: 'verified', verified: true, ...(components ? { components } : {}) };
  } catch {
    return { status: 'unreachable', verified: false };
  } finally {
    clearTimeout(timeout);
  }
}

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

/** Sign one Gateway request; renderer code never receives this key or proof.
 *
 * The proof binds ``timestamp`` + a fresh one-time ``nonce`` + method + pathname +
 * body hash. The gateway consumes the nonce after a successful verify, so the same
 * proof cannot be replayed within its TTL even for an identical request (H-19).
 * Callers must materialize streaming/multipart requests before signing so there
 * is no unbound payload exception.
 */
export function createDesktopSecurityProof(
  method: string,
  pathname: string,
  body: string | Buffer,
  options: {
    crewHome: string;
    instanceKey?: Buffer;
    nowSeconds?: number;
    nonce?: string;
  },
): string {
  if (!pathname.startsWith('/api/') && !pathname.startsWith('/ws')) {
    throw new Error('invalid security proof path');
  }
  if (!path.isAbsolute(options.crewHome)) {
    throw new Error('security proof crew home must be absolute');
  }
  const timestamp = Math.floor(options.nowSeconds ?? Date.now() / 1000);
  const nonce = options.nonce ?? randomBytes(16).toString('hex');
  const plainBodyHash = createHash('sha256').update(body).digest('hex');
  const message = Buffer.concat([
    SECURITY_PROOF_CONTEXT,
    Buffer.from(`${timestamp}\n${nonce}\n${method.toUpperCase()}\n${pathname}\n${plainBodyHash}`, 'utf8'),
  ]);
  const key = gatewayInstanceKey(options.crewHome, options.instanceKey);
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
