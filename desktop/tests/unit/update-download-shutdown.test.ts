import * as fs from 'node:fs/promises';
import * as http from 'node:http';
import * as os from 'node:os';
import * as path from 'node:path';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const { userData } = vi.hoisted(() => ({ userData: { path: '' } }));
vi.mock('electron', () => ({ app: { getPath: () => userData.path } }));

let server: http.Server | undefined;

beforeEach(async () => {
  userData.path = await fs.mkdtemp(path.join(os.tmpdir(), 'ace-download-quit-'));
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
  const part = path.join(userData.path, 'updates', 'ace-test.exe.part');
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
