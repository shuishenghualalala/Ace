import { spawn, type ChildProcessWithoutNullStreams } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

export type NearbyCommand =
  | { type: 'start_discovery' }
  | { type: 'stop_discovery' }
  | { type: 'set_discoverable'; enabled: boolean }
  | { type: 'connect_peer'; peer_id: string }
  | { type: 'disconnect_peer'; peer_id: string }
  | { type: 'send_message'; peer_id: string; text: string }
  | { type: 'create_room'; room_id: string; room_name: string; peer_ids: string[] }
  | {
    type: 'send_room_message';
    room_id: string;
    text: string;
    mentions?: string[];
    reply_to?: NearbyReplyReference;
  }
  | {
    type: 'send_room_file';
    room_id: string;
    file_id: string;
    name: string;
    mime_type: string;
    size: number;
    sha256: string;
    data_base64: string;
    mentions?: string[];
    reply_to?: NearbyReplyReference;
  }
  | { type: 'leave_room'; room_id: string }
  | { type: 'shutdown' };

export interface NearbyReplyReference {
  message_id: string;
  sender: string;
  text: string;
}

export interface NearbyEvent {
  type: string;
  [key: string]: unknown;
}

export interface NearbyServiceOptions {
  repoRoot: string;
  resourcesPath: string;
  isPackaged: boolean;
  crewHome: string;
  onEvent: (event: NearbyEvent) => void;
}

interface NearbyProcessCommand {
  command: string;
  args: string[];
  cwd: string;
}

export class NearbyService {
  private child: ChildProcessWithoutNullStreams | null = null;
  private startPromise: Promise<void> | null = null;
  private stopping = false;
  private ready = false;

  public constructor(private readonly options: NearbyServiceOptions) {}

  public async start(): Promise<void> {
    if (this.ready && this.child) return;
    if (this.startPromise) return this.startPromise;

    this.startPromise = new Promise<void>((resolve, reject) => {
      let settled = false;
      let command: NearbyProcessCommand;
      try {
        command = this.resolveCommand();
      } catch (error) {
        console.error('[nearby] failed to resolve runtime:', error);
        reject(error);
        return;
      }

      console.warn(
        '[nearby] starting runtime command=' + command.command
          + ' args=' + command.args.join(' ')
          + ' packaged=' + this.options.isPackaged,
      );
      const child = spawn(command.command, command.args, {
        cwd: command.cwd,
        env: {
          ...process.env,
          CREW_HOME: this.options.crewHome,
          CREW_NEARBY_IPC: '1',
        },
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
      });
      this.child = child;
      this.stopping = false;
      console.warn('[nearby] runtime spawned pid=' + (child.pid ?? 'unknown'));

      child.stdout.setEncoding('utf8');
      child.stderr.setEncoding('utf8');
      let pending = '';
      child.stdout.on('data', (chunk: string) => {
        pending += chunk;
        const lines = pending.split(/\r?\n/);
        pending = lines.pop() ?? '';
        for (const line of lines) this.handleLine(line, () => {
          if (settled) return;
          settled = true;
          this.ready = true;
          console.warn('[nearby] runtime reported ready');
          resolve();
        });
      });
      child.stderr.on('data', (chunk: string) => {
        for (const text of String(chunk).split(/\r?\n/).map((line) => line.trim()).filter(Boolean)) {
          console.warn('[nearby][runtime]', text);
        }
      });
      child.once('error', (error) => {
        console.error('[nearby] runtime process error:', error);
        if (!settled) {
          settled = true;
          reject(error);
        }
        this.finish(child);
      });
      child.once('exit', (code, signal) => {
        console.warn(
          '[nearby] runtime exited code=' + (code ?? 'none') + ' signal=' + (signal ?? 'none'),
        );
        if (!settled) {
          settled = true;
          reject(new Error(`Nearby process exited before ready (code=${code}, signal=${signal ?? 'none'})`));
        }
        this.finish(child);
      });
    });

    try {
      await this.startPromise;
    } finally {
      this.startPromise = null;
    }
  }

  public async send(command: NearbyCommand): Promise<void> {
    await this.start();
    if (!this.child?.stdin.writable) throw new Error('Nearby process is not available');
    console.warn('[nearby] sending command type=' + command.type);
    this.child.stdin.write(`${JSON.stringify(command)}\n`);
  }

  public async stop(): Promise<void> {
    const child = this.child;
    if (!child) return;
    console.warn('[nearby] stopping runtime');
    this.stopping = true;
    this.ready = false;
    try {
      if (child.stdin.writable) child.stdin.write(`${JSON.stringify({ type: 'shutdown' })}\n`);
    } catch {
      // The process may already be closing.
    }
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        if (!child.killed) child.kill();
        resolve();
      }, 1_000);
      child.once('exit', () => {
        clearTimeout(timer);
        resolve();
      });
    });
    this.finish(child);
  }

  private handleLine(line: string, onReady: () => void): void {
    const trimmed = line.trim();
    if (!trimmed) return;
    if (!trimmed.startsWith('{')) {
      console.warn('[nearby][runtime-stdout]', trimmed);
      return;
    }
    try {
      const event = JSON.parse(trimmed) as NearbyEvent;
      if (!event || typeof event.type !== 'string') return;
      if (event.type === 'ready') onReady();
      this.options.onEvent(event);
    } catch (error) {
      console.warn('[nearby] ignored malformed event:', error);
    }
  }

  private finish(child: ChildProcessWithoutNullStreams): void {
    if (this.child !== child) return;
    this.child = null;
    this.ready = false;
    if (!this.stopping) {
      this.options.onEvent({ type: 'error', message: 'Nearby 服务已退出' });
    }
  }

  private resolveCommand(): NearbyProcessCommand {
    const configured = String(process.env['CREW_NEARBY_BIN'] ?? '').trim();
    const binaryName = process.platform === 'win32' ? 'crew-nearby.exe' : 'crew-nearby';
    const candidates = configured
      ? [configured]
      : this.options.isPackaged
        ? [path.join(this.options.resourcesPath, 'crew-nearby', binaryName)]
        : [path.join(this.options.repoRoot, 'nearby', 'target', 'debug', binaryName)];
    const binary = candidates.find((candidate) => path.isAbsolute(candidate) && fs.existsSync(candidate));
    if (binary) return { command: binary, args: ['--ipc'], cwd: path.dirname(binary) };
    if (this.options.isPackaged) {
      throw new Error(`找不到 Nearby 运行时：${candidates.join(', ')}`);
    }
    return {
      command: process.platform === 'win32' ? 'cargo.exe' : 'cargo',
      args: ['run', '--quiet', '--manifest-path', path.join(this.options.repoRoot, 'nearby', 'Cargo.toml'), '--', '--ipc'],
      cwd: this.options.repoRoot,
    };
  }
}
