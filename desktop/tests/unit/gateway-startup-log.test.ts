import assert from 'node:assert/strict';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { once } from 'node:events';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { setTimeout as delay } from 'node:timers/promises';
import { afterEach, beforeEach, describe, it } from 'vitest';
import { GatewayStartupLog } from '../../src/main/gateway-startup-log';

const MAX_BYTES = 2 * 1024 * 1024;
const children: ChildProcessWithoutNullStreams[] = [];
let directory: string;
let file: string;

function start(script: string) {
  const child = spawn(process.execPath, ['-e', script], { stdio: 'pipe', windowsHide: true });
  children.push(child);
  return child;
}

const flood = `
  const { once } = require('node:events');
  (async () => {
    const block = Buffer.from('界'.repeat(4096) + '\\n');
    for (let i=0; i<850; i++) {
      const output = i % 2 ? process.stdout : process.stderr;
      if (!output.write(block)) await once(output, 'drain');
    }
    process.stdout.write('STDOUT_DONE\\n');
    process.stderr.write('STDERR_DONE\\n');
  })().catch(error => { console.error(error); process.exitCode=1; });
`;

async function readBoundedLogs(): Promise<string> {
  const names = await fs.readdir(directory);
  assert.equal(names.length, 3);
  const chunks: string[] = [];
  for (const name of [file + '.2', file + '.1', file]) {
    const content = await fs.readFile(name);
    assert.ok(content.length <= MAX_BYTES);
    chunks.push(new TextDecoder('utf-8', { fatal: true }).decode(content));
  }
  return chunks.join('');
}

describe('Gateway startup log', () => {
  beforeEach(async () => {
    directory = await fs.mkdtemp(path.join(os.tmpdir(), 'ace-gateway-log-'));
    file = path.join(directory, 'gateway-startup.log');
  });
  afterEach(async () => {
    for (const child of children.splice(0)) {
      if (child.exitCode === null && child.signalCode === null) child.kill();
    }
    await fs.rm(directory, { recursive: true, force: true });
  });

  it('rotates sustained output from a live Gateway and drains both streams before exit', { timeout: 20_000 }, async () => {
    const log = new GatewayStartupLog(file);
    const child = start(flood);
    log.attach(child);
    await log.flushAfterChildren();
    assert.equal(child.exitCode, 0);
    const text = await readBoundedLogs();
    assert.ok(text.includes('STDOUT_DONE'));
    assert.ok(text.includes('STDERR_DONE'));
    assert.ok(text.includes('[out]'));
    assert.ok(text.includes('[err]'));
    assert.match(text.trimEnd(), /\[exit\] code=0 signal=null pid=\d+$/);
  });

  it('keeps draining the real pipes and bounds pending writes while storage stalls', { timeout: 20_000 }, async () => {
    let unblock!: () => void;
    const stalled = new Promise<void>(resolve => { unblock = resolve; });
    const writes: number[] = [];
    const log = new GatewayStartupLog(file, {
      ...fs,
      appendFile: async (name, data, options) => {
        writes.push(Buffer.byteLength(data as Buffer));
        await stalled;
        return fs.appendFile(name, data, options);
      },
    });
    const child = start(flood);
    log.attach(child);
    try {
      await once(child, 'close');
      assert.equal(child.exitCode, 0);
      assert.equal(writes.length, 1);
    } finally { unblock(); }
    await log.flushAfterChildren();
    assert.ok(Math.max(...writes) < 513 * 1024);
    const text = await fs.readFile(file, 'utf8');
    assert.match(text, /\d+ log bytes omitted or unconfirmed/);
    assert.ok(text.includes(`[exit] code=0 signal=null pid=${child.pid}`));
  });

  it('isolates disk failure, then reopens and records the gap and the next child', async () => {
    const warnings: string[] = [];
    let blocked = true;
    const log = new GatewayStartupLog(file, {
      ...fs,
      appendFile: async (name, data, options) => {
        if (blocked) throw Object.assign(new Error('disk unavailable'), { code: 'ENOSPC' });
        return fs.appendFile(name, data, options);
      },
    }, message => warnings.push(message));
    const first = start("process.stderr.write('first');process.exitCode=7");
    log.attach(first);
    await log.flushAfterChildren();
    assert.equal(first.exitCode, 7);
    assert.equal(warnings.length, 1);
    assert.ok(warnings[0]!.includes('ENOSPC'));
    blocked = false;
    await delay(1050);
    const second = start("process.stdout.write('second')");
    log.attach(second);
    await log.flushAfterChildren();
    const text = await fs.readFile(file, 'utf8');
    assert.match(text, /\d+ log bytes omitted or unconfirmed/);
    assert.ok(text.includes(`[pid=${second.pid}] [out] second`));
    assert.ok(text.includes(`[exit] code=0 signal=null pid=${second.pid}`));
  });

  it('keeps bounded tails of oversized legacy files and preserves split UTF-8 code points', async () => {
    const seed = '界'.repeat(800_000);
    await Promise.all([file, file + '.1', file + '.2'].map(name => fs.writeFile(name, seed)));
    const log = new GatewayStartupLog(file);
    const split = start("const b=Buffer.from('🙂'); process.stdout.write(b.subarray(0, 2)); setTimeout(()=>process.stdout.write(b.subarray(2)), 20)");
    log.attach(split);
    await log.flushAfterChildren();
    const text = await readBoundedLogs();
    assert.ok(text.includes('🙂'));
    assert.ok(!text.includes('�'));
  });
  it('preserves both child identities across overlapping process generations', async () => {
    const log = new GatewayStartupLog(file);
    const first = start("setTimeout(()=>process.stderr.write('first-late'),150)");
    log.attach(first);
    const second = start("process.stdout.write('second')");
    log.attach(second);
    await log.flushAfterChildren();
    const text = await fs.readFile(file, 'utf8');
    assert.ok(text.includes(`[pid=${first.pid}] [err] first-late`));
    assert.ok(text.includes(`[pid=${second.pid}] [out] second`));
    for (const child of [first, second]) assert.ok(text.includes(`[exit] code=0 signal=null pid=${child.pid}`));
  });

  it('stops appending when rotation is blocked and retries after the lock clears', async () => {
    await fs.writeFile(file, Buffer.alloc(MAX_BYTES, 65));
    let locked = true;
    const warnings: string[] = [];
    const log = new GatewayStartupLog(file, {
      ...fs,
      rename: (from, to) => {
        if (from === file && locked) return Promise.reject(Object.assign(new Error('file busy'), { code: 'EBUSY' }));
        return fs.rename(from, to);
      },
    }, message => warnings.push(message));
    const first = start("process.stdout.write('locked')");
    log.attach(first);
    await log.flushAfterChildren();
    assert.equal((await fs.stat(file)).size, MAX_BYTES);
    assert.match(warnings[0]!, /EBUSY/);
    locked = false;
    await delay(1050);
    const next = start("process.stderr.write('after-lock')");
    log.attach(next);
    await log.flushAfterChildren();
    assert.equal((await fs.stat(file + '.1')).size, MAX_BYTES);
    assert.ok((await fs.readFile(file, 'utf8')).includes('after-lock'));
  });

});
