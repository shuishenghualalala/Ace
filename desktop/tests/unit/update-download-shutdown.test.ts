import * as fs from 'node:fs/promises';
import * as http from 'node:http';
import * as os from 'node:os';
import * as path from 'node:path';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const { userData } = vi.hoisted(() => ({ userData: { path: '', failReceipt: false } }));
vi.mock('fs', async (importOriginal) => {
  const original = await importOriginal<typeof import('node:fs')>();
  return {
    ...original,
    renameSync(from: string, to: string) {
      if (userData.failReceipt && path.basename(to) === 'update-state.json') throw new Error('fixture disk full');
      original.renameSync(from, to);
    },
  };
});
vi.mock('electron', () => ({ app: { getPath: () => userData.path } }));

let server: http.Server | undefined;

beforeEach(async () => {
  userData.path = await fs.mkdtemp(path.join(os.tmpdir(), 'ace-download-quit-'));
  userData.failReceipt = false;
  vi.resetModules();
});

afterEach(async () => {
  if (server?.listening) {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server!.close(() => resolve()));
  }
  server = undefined;
  await fs.rm(userData.path, { recursive: true, force: true });
});

it('closes the real partial-file writer before quit and keeps the package resumable', async () => {
  const updates = await import('../../src/main/update/download-controller');
  const { readUpdateState } = await import('../../src/main/update/update-state');
  updates.configureUpdateController(null, () => false);
  const prefix = Buffer.from('MZreal-prefix');
  const suffix = Buffer.from('-remaining-data');
  const connectionClosed = Promise.withResolvers<void>();
  const downloaded = Promise.withResolvers<void>();
  const requests: Array<string | undefined> = [];
  server = http.createServer((request, response) => {
    requests.push(request.headers.range);
    if (requests.length === 1) {
      response.writeHead(200, { 'Content-Length': prefix.length + suffix.length });
      response.write(prefix);
      response.once('close', () => connectionClosed.resolve());
    } else {
      response.writeHead(206, { 'Content-Length': suffix.length });
      response.end(suffix);
    }
  });
  await new Promise<void>((resolve) => server!.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Missing fixture port');
  const url = `http://127.0.0.1:${address.port}/ace-test.exe`;
  updates.configureUpdateController((progress) => {
    if (progress.phase === 'downloaded') downloaded.resolve();
  }, () => false);
  expect(updates.startDownload({ version: '42.0.0', type: 'reminder', url }).success).toBe(true);
  const [part] = updates.activeFilePaths();
  if (!part) throw new Error('Missing active download path');
  await vi.waitFor(async () => expect(await fs.readFile(part)).toEqual(prefix));

  await Promise.all([updates.disposeForQuit(), connectionClosed.promise]);
  expect(readUpdateState().downloaded).toBeNull();
  expect(await fs.readFile(part)).toEqual(prefix);
  const renamed = `${part}.check-closed`;
  await fs.rename(part, renamed);
  await fs.rename(renamed, part);

  expect(updates.resumeDownload().success).toBe(true);
  await downloaded.promise;
  expect(requests).toEqual([undefined, `bytes=${prefix.length}-`]);
  const result = readUpdateState().downloaded;
  expect(result?.size).toBe(prefix.length + suffix.length);
  expect(await fs.readFile(result!.filePath)).toEqual(Buffer.concat([prefix, suffix]));
});

it('preserves the previous package and receipt until replacement is verified and committed', async () => {
  const updates = await import('../../src/main/update/download-controller');
  const storage = await import('../../src/main/update/update-state');
  let status = 200;
  let bytes = Buffer.from('MZ-release-one');
  server = http.createServer((_req, res) => {
    res.writeHead(status, { 'Content-Length': bytes.length });
    res.end(bytes);
  });
  await new Promise<void>((resolve) => server!.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Missing fixture port');
  const url = `http://127.0.0.1:${address.port}/installer.exe`;
  async function download(version: string, expected: 'downloaded' | 'error') {
    const done = Promise.withResolvers<void>();
    updates.configureUpdateController((event) => {
      if (event.phase === 'downloaded' || event.phase === 'error') {
        if (event.phase === expected) done.resolve();
        else done.reject(new Error(`Unexpected ${event.phase}: ${event.message}`));
      }
    }, () => false);
    updates.startDownload({ version, url, type: 'reminder' });
    await done.promise;
  }
  await download('2.0.0', 'downloaded');
  const old = storage.readUpdateState().downloaded!;
  const previouslyReady = await fs.readFile(old.filePath);

  updates.configureUpdateController(null, () => true);
  expect(updates.startDownload({ version: '3.0.0', url, type: 'force' }).success).toBe(false);
  status = 503;
  await download('3.0.0', 'error');
  status = 200;
  bytes = Buffer.from('invalid package');
  await download('3.0.0', 'error');
  bytes = Buffer.from('MZ-release-three');
  userData.failReceipt = true;
  await download('3.0.0', 'error');
  userData.failReceipt = false;
  expect(await fs.readFile(old.filePath)).toEqual(previouslyReady);
  expect(storage.installableUpdateState('1.0.0').downloaded).toEqual(old);
  expect((await fs.readdir(userData.path)).filter((name) => name.endsWith('.tmp'))).toEqual([]);

  storage.setForceLock({ requiredVersion: '3.0.0', url });
  vi.resetModules();
  const restored = await import('../../src/main/update/update-state');
  expect(restored.installableUpdateState('1.0.0')).toEqual({ downloaded: null, forceLock: { requiredVersion: '3.0.0', url } });
  expect(restored.readUpdateState().downloaded).toEqual(old);
  await download('3.0.0', 'downloaded');
  const upgraded = restored.installableUpdateState('1.0.0').downloaded!;
  expect(upgraded.version).toBe('3.0.0');
  expect(upgraded.filePath).not.toBe(old.filePath);
  expect(await fs.readFile(upgraded.filePath)).toEqual(bytes);
  await expect(fs.stat(old.filePath)).rejects.toMatchObject({ code: 'ENOENT' });
  await download('2.0.0', 'error');
  expect(restored.readUpdateState().downloaded).toEqual(upgraded);
  expect(restored.installableUpdateState('3.0.0')).toEqual({ downloaded: null, forceLock: null });
});
