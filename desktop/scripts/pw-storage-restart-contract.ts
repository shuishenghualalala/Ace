import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { app, BrowserWindow } from 'electron';

import { BrowserHost } from '../src/main/browser-host';

const MARKER = 'persisted-account-a';
const CHILD_TIMEOUT_MS = 60_000;
const scriptPath = fileURLToPath(import.meta.url);

interface AccountIdentity {
  runtimeKey: string;
  profile: string;
}

async function deadline<T>(operation: Promise<T>, timeoutMs: number, label: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error(`Timed out: ${label}`)), timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function account(root: string, name: string): AccountIdentity {
  const digest = createHash('sha256').update(name).digest('hex');
  return {
    runtimeKey: `crew_${digest.slice(0, 12)}`,
    profile: path.join(root, 'accounts', `acct_${digest.slice(0, 16)}`, 'browser', 'profile'),
  };
}

async function availablePort(): Promise<number> {
  const reservation = createServer();
  await new Promise<void>((resolve, reject) => {
    reservation.once('error', reject);
    reservation.listen(0, '127.0.0.1', resolve);
  });
  const address = reservation.address();
  if (!address || typeof address === 'string') throw new Error('No available loopback port');
  await new Promise<void>((resolve) => reservation.close(() => resolve()));
  return address.port;
}

async function runChildProcess(
  phase: 'write' | 'read',
  root: string,
  port: number,
): Promise<number> {
  const env = { ...process.env };
  delete env.ELECTRON_RUN_AS_NODE;
  const child = spawn(process.execPath, [scriptPath, '--child', phase, root, String(port)], {
    stdio: 'inherit',
    env,
  });
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    child.kill();
  }, CHILD_TIMEOUT_MS);
  try {
    const code = await new Promise<number | null>((resolve, reject) => {
      child.once('error', reject);
      child.once('close', resolve);
    });
    if (timedOut) throw new Error(`Timed out: ${phase} Electron process`);
    if (code !== 0) throw new Error(`${phase} Electron process exited with ${String(code)}`);
    return child.pid ?? -1;
  } finally {
    clearTimeout(timer);
  }
}

async function runParent(): Promise<void> {
  const port = await availablePort();
  const root = await mkdtemp(path.join(os.tmpdir(), 'crew-pw-storage-restart-'));
  try {
    const writePid = await runChildProcess('write', root, port);
    const readPid = await runChildProcess('read', root, port);
    if (writePid === readPid) throw new Error('Storage phases did not use distinct processes');
    console.log(`PASS BrowserHost 跨进程站点存储重启契约；write PID ${writePid}；read PID ${readPid}`);
  } finally {
    await rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  }
}

function writeExpression(): string {
  return `async () => {
    const marker = ${JSON.stringify(MARKER)};
    localStorage.setItem('crew_storage_restart', marker);
    document.cookie = 'crew_storage_restart=' + marker
      + '; path=/; Max-Age=3600; SameSite=Lax';
    const db = await new Promise((resolve, reject) => {
      const request = indexedDB.open('crew-storage-restart', 1);
      request.onupgradeneeded = () => request.result.createObjectStore('values');
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    await new Promise((resolve, reject) => {
      const transaction = db.transaction('values', 'readwrite');
      transaction.objectStore('values').put(marker, 'marker');
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
    db.close();
    return 'stored';
  }`;
}

function readExpression(): string {
  return `async () => {
    const local = localStorage.getItem('crew_storage_restart');
    const pair = document.cookie.split('; ').find(
      (entry) => entry.startsWith('crew_storage_restart='),
    );
    const cookie = pair ? pair.slice('crew_storage_restart='.length) : null;
    const db = await new Promise((resolve, reject) => {
      const request = indexedDB.open('crew-storage-restart', 1);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    if (!db.objectStoreNames.contains('values')) {
      db.close();
      return { local, cookie, indexed: null };
    }
    const indexed = await new Promise((resolve, reject) => {
      const request = db.transaction('values', 'readonly').objectStore('values').get('marker');
      request.onsuccess = () => resolve(request.result ?? null);
      request.onerror = () => reject(request.error);
    });
    db.close();
    return { local, cookie, indexed };
  }`;
}

async function runChild(phase: 'write' | 'read', root: string, port: number): Promise<void> {
  const server = createServer((_request, response) => {
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    response.end('<!doctype html><title>Storage restart</title><p>storage restart</p>');
  });
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', resolve);
  });
  const address = server.address();
  if (!address || typeof address === 'string' || address.port !== port) {
    throw new Error('Storage server did not reuse the requested origin port');
  }
  const pageURL = `http://127.0.0.1:${port}/`;
  const proxy = createServer(async (request, response) => {
    try {
      if (!request.url || request.method === 'CONNECT') {
        response.writeHead(405).end();
        return;
      }
      const upstream = await fetch(new URL(request.url), {
        method: request.method,
        redirect: 'manual',
      });
      response.writeHead(upstream.status, {
        'Content-Type': upstream.headers.get('content-type') ?? 'application/octet-stream',
        'Cache-Control': 'no-store',
      });
      response.end(Buffer.from(await upstream.arrayBuffer()));
    } catch (error) {
      response.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' });
      response.end(error instanceof Error ? error.message : 'proxy failure');
    }
  });
  await new Promise<void>((resolve, reject) => {
    proxy.once('error', reject);
    proxy.listen(0, '127.0.0.1', resolve);
  });
  const proxyAddress = proxy.address();
  if (!proxyAddress || typeof proxyAddress === 'string') throw new Error('Storage proxy has no TCP port');
  const proxyURL = `http://127.0.0.1:${proxyAddress.port}`;
  const accountA = account(root, 'pw-storage-restart-a');
  const accountB = account(root, 'pw-storage-restart-b');
  await mkdir(accountA.profile, { recursive: true });
  await mkdir(accountB.profile, { recursive: true });
  const panel = new BrowserWindow({
    show: false,
    width: 1100,
    height: 800,
    webPreferences: { sandbox: true },
  });
  const host = new BrowserHost(() => panel);
  const newTab = async (identity: AccountIdentity, sessionName: string): Promise<string> => {
    const sessionHash = createHash('sha256').update(sessionName).digest('hex').slice(0, 32);
    const created = await deadline(host.handleRpc({
      runtime_key: identity.runtimeKey,
      method: 'execute',
      params: {
        profile_dir: identity.profile,
        proxy_url: proxyURL,
        command: 'tab',
        args: ['new', '--label', `s${sessionHash}-1`, pageURL],
        mutating: true,
        command_timeout_ms: 15_000,
      },
    }), 20_000, `${phase} create tab`) as { data?: { targetId?: string } };
    const targetId = String(created.data?.targetId ?? '');
    if (!targetId) throw new Error(`${phase} did not create a tab`);
    return targetId;
  };
  const evaluate = async (
    identity: AccountIdentity,
    targetId: string,
    expression: string,
  ): Promise<unknown> => {
    const response = await deadline(host.handleRpc({
      runtime_key: identity.runtimeKey,
      method: 'execute',
      params: {
        profile_dir: identity.profile,
        proxy_url: proxyURL,
        target_id: targetId,
        command: 'eval',
        args: [expression],
        command_timeout_ms: 15_000,
      },
    }), 20_000, `${phase} evaluate`) as { data?: { serialized?: string } };
    return JSON.parse(String(response.data?.serialized));
  };
  try {
    if (phase === 'write') {
      const targetId = await newTab(accountA, 'pw-storage-restart-a1');
      const stored = await evaluate(accountA, targetId, writeExpression());
      if (stored !== 'stored') throw new Error(`Write phase failed: ${String(stored)}`);
      console.log('WRITE_OK Cookie/localStorage/IndexedDB');
      return;
    }
    const sharedTarget = await newTab(accountA, 'pw-storage-restart-a2');
    const shared = await evaluate(accountA, sharedTarget, readExpression());
    const expected = { local: MARKER, cookie: MARKER, indexed: MARKER };
    if (JSON.stringify(shared) !== JSON.stringify(expected)) {
      throw new Error(`Restarted account storage mismatch: ${JSON.stringify(shared)}`);
    }
    const isolatedTarget = await newTab(accountB, 'pw-storage-restart-b1');
    const isolated = await evaluate(accountB, isolatedTarget, readExpression());
    const empty = { local: null, cookie: null, indexed: null };
    if (JSON.stringify(isolated) !== JSON.stringify(empty)) {
      throw new Error(`Cross-account storage leaked: ${JSON.stringify(isolated)}`);
    }
    console.log('RESTART_OK A2 persisted Cookie/localStorage/IndexedDB；B1 empty');
  } finally {
    try {
      await deadline(host.dispose(), 10_000, `${phase} host dispose`);
    } finally {
      if (!panel.isDestroyed()) panel.destroy();
      server.closeAllConnections();
      proxy.closeAllConnections();
      await Promise.all([
        new Promise<void>((resolve) => server.close(() => resolve())),
        new Promise<void>((resolve) => proxy.close(() => resolve())),
      ]);
    }
  }
}

const childIndex = process.argv.indexOf('--child');
if (childIndex >= 0) {
  const phase = process.argv[childIndex + 1];
  const root = process.argv[childIndex + 2];
  const port = Number(process.argv[childIndex + 3]);
  if (
    (phase !== 'write' && phase !== 'read')
    || !root
    || !Number.isInteger(port)
    || port <= 0
    || port > 65_535
  ) {
    throw new Error('Child mode requires --child <write|read> <root> <port>');
  }
  app.on('window-all-closed', () => undefined);
  app.whenReady()
    .then(() => runChild(phase, root, port))
    .then(() => app.quit())
    .catch((error: unknown) => {
      console.error(error instanceof Error ? error.message : String(error));
      app.exit(1);
    });
} else {
  runParent()
    .then(() => app.exit(0))
    .catch((error: unknown) => {
      console.error(error instanceof Error ? error.message : String(error));
      app.exit(1);
    });
}
