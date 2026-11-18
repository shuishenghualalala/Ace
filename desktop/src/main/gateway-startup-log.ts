import type { ChildProcessWithoutNullStreams } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { promises as fs } from 'node:fs';
import path from 'node:path';

const FILE_LIMIT = 2 * 1024 * 1024;
const QUEUE_LIMIT = 512 * 1024;
const PAUSE_MS = 100;
const RETRY_MS = 1000;

type Files = Pick<typeof fs, 'mkdir' | 'stat' | 'rename' | 'appendFile' | 'open' | 'writeFile' | 'rm'>;

function missing(error: unknown): boolean {
  return (error as NodeJS.ErrnoException)?.code === 'ENOENT';
}

async function fileSize(files: Files, file: string): Promise<number> {
  try { return (await files.stat(file)).size; }
  catch (error) { if (missing(error)) return 0; throw error; }
}

function atCharacterBoundary(bytes: Buffer, offset: number): number {
  while (offset > 0 && (bytes[offset]! & 0xc0) === 0x80) offset--;
  return offset;
}

/** Shared by consecutive Gateway generations. At most one disk operation owns the file set. */
export class GatewayStartupLog {
  private queue: Buffer[] = [];
  private pendingBytes = 0;
  private skippedBytes = 0;
  private retryAt = 0;
  private ready = false;
  private writing: Promise<void> | null = null;
  private pauseTimer: NodeJS.Timeout | undefined;
  private shedding = false;
  private readonly children = new Map<ChildProcessWithoutNullStreams, Promise<void>>();

  constructor(
    readonly file: string,
    private readonly files: Files = fs,
    private readonly warn: (message: string) => void = console.warn,
  ) {}

  attach(child: ChildProcessWithoutNullStreams, consoleLabel?: string): void {
    const pid = child.pid ?? 'unknown';
    const closed = new Promise<void>(resolve => {
      child.once('close', (code, signal) => {
        this.line(`[exit] code=${code} signal=${signal} pid=${pid}`);
        this.children.delete(child);
        resolve();
      });
    });
    this.children.set(child, closed);
    this.line(`===== gateway spawn pid=${pid} =====`);
    for (const [tag, input, mirror] of [
      ['out', child.stdout, process.stdout], ['err', child.stderr, process.stderr],
    ] as const) {
      input.setEncoding('utf8');
      input.on('data', (chunk: string) => {
        this.enqueue(`[pid=${pid}] [${tag}] ${chunk}`, QUEUE_LIMIT - 4096);
        if (consoleLabel && !mirror.writableNeedDrain && !mirror.destroyed) {
          if (tag === 'out') console.log(consoleLabel, chunk.trim());
          else console.warn(consoleLabel, chunk.trim());
        }
      });
      if (this.pauseTimer) input.pause();
    }
  }

  line(text: string): void {
    this.enqueue(text, QUEUE_LIMIT);
  }

  private enqueue(text: string, budget: number): void {
    const record = Buffer.from(`[${new Date().toISOString()}] ${text}${text.endsWith('\n') ? '' : '\n'}`);
    if (Date.now() < this.retryAt || record.length > budget - this.pendingBytes) {
      this.skippedBytes += record.length;
      return;
    }
    this.queue.push(record);
    this.pendingBytes += record.length;
    if (this.pendingBytes >= QUEUE_LIMIT / 2 && !this.pauseTimer && !this.shedding) {
      for (const child of this.children.keys()) { child.stdout.pause(); child.stderr.pause(); }
      // Slow or stuck storage must not stall the Gateway's own event loop indefinitely.
      this.pauseTimer = setTimeout(() => {
        this.shedding = true;
        this.resumeInputs();
      }, PAUSE_MS);
    }
    this.writing ??= Promise.resolve().then(() => this.drain());
  }

  private resumeInputs(): void {
    if (this.pauseTimer) clearTimeout(this.pauseTimer);
    this.pauseTimer = undefined;
    for (const child of this.children.keys()) { child.stdout.resume(); child.stderr.resume(); }
  }

  private async drain(): Promise<void> {
    try {
      while (this.queue.length || this.skippedBytes) {
        const records = this.queue;
        this.queue = [];
        const dropped = this.skippedBytes;
        this.skippedBytes = 0;
        const count = records.reduce((size, record) => size + record.length, 0);
        if (dropped) records.push(Buffer.from(`[gateway-log] ${dropped} log bytes omitted or unconfirmed\n`));
        try {
          await this.append(Buffer.concat(records));
        } catch (error) {
          this.skippedBytes += this.pendingBytes + dropped;
          this.queue = [];
          this.pendingBytes = 0;
          this.ready = false;
          this.retryAt = Date.now() + RETRY_MS;
          this.warn(`[gateway-log] write failed (${(error as NodeJS.ErrnoException).code ?? 'I/O'}), ${this.skippedBytes} log bytes unconfirmed`);
          break;
        }
        this.pendingBytes -= count;
        if (this.pendingBytes < QUEUE_LIMIT / 2) {
          this.shedding = false;
          if (this.pauseTimer) this.resumeInputs();
        }
      }
    } finally {
      this.writing = null;
      this.shedding = false;
      if (this.pauseTimer) this.resumeInputs();
    }
  }

  /** Preserve the most recent bytes from oversized logs left by older Desktop builds. */
  private async trimLegacy(file: string): Promise<void> {
    const size = await fileSize(this.files, file);
    if (size <= FILE_LIMIT) return;
    const handle = await this.files.open(file, 'r');
    const tail = Buffer.alloc(FILE_LIMIT);
    let used = 0;
    try {
      while (used < tail.length) {
        const { bytesRead } = await handle.read(tail, used, tail.length - used, size - tail.length + used);
        if (!bytesRead) break;
        used += bytesRead;
      }
    } finally { await handle.close(); }
    let start = 0;
    while (start < used && (tail[start]! & 0xc0) === 0x80) start++;
    const temporary = file + '.' + randomUUID() + '.trim';
    try {
      await this.files.writeFile(temporary, tail.subarray(start, used), { flag: 'wx', mode: 0o600 });
      await this.files.rename(temporary, file);
    } finally { await this.files.rm(temporary, { force: true }); }
  }

  private async append(bytes: Buffer): Promise<void> {
    if (!this.ready) {
      await this.files.mkdir(path.dirname(this.file), { recursive: true });
      for (const name of [this.file, this.file + '.1', this.file + '.2']) await this.trimLegacy(name);
      this.ready = true;
    }
    let size = await fileSize(this.files, this.file);
    let offset = 0;
    while (offset < bytes.length) {
      const end = atCharacterBoundary(bytes, Math.min(bytes.length, offset + FILE_LIMIT - size));
      if (end <= offset) {
        try { await this.files.rename(this.file + '.1', this.file + '.2'); }
        catch (error) { if (!missing(error)) throw error; }
        await this.files.rename(this.file, this.file + '.1');
        size = 0;
        continue;
      }
      await this.files.appendFile(this.file, bytes.subarray(offset, end), { mode: 0o600 });
      size += end - offset;
      offset = end;
    }
  }

  async flushAfterChildren(): Promise<void> {
    await Promise.all(this.children.values());
    await this.writing;
  }
}
