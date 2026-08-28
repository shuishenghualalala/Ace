import { spawn, type ChildProcessWithoutNullStreams } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

export type NearbyRoomAgentMode = 'mention' | 'auto' | 'quiet';

export type NearbyCommand =
  | { type: 'start_discovery' }
  | { type: 'stop_discovery' }
  | { type: 'set_discoverable'; enabled: boolean }
  | { type: 'connect_peer'; peer_id: string }
  | { type: 'disconnect_peer'; peer_id: string }
  | { type: 'send_agent_request'; peer_id: string; text: string }
  | { type: 'send_agent_reply'; peer_id: string; request_id: string; text: string; error: boolean }
  | { type: 'send_peer_message'; peer_id: string; text: string; client_message_id?: string; mentions?: string[] }
  | {
    type: 'send_peer_file';
    peer_id: string;
    file_id: string;
    name: string;
    mime_type: string;
    size: number;
    sha256: string;
    file_path: string;
    client_message_id?: string;
  }
  | { type: 'create_room'; room_id: string; room_name: string; peer_ids: string[]; agent_mode?: NearbyRoomAgentMode }
  | { type: 'invite_to_room'; room_id: string; peer_ids: string[] }
  | {
    type: 'send_room_message';
    room_id: string;
    text: string;
    client_message_id?: string;
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
    file_path: string;
    client_message_id?: string;
    mentions?: string[];
    reply_to?: NearbyReplyReference;
  }
  | { type: 'leave_room'; room_id: string }
  | { type: 'respond_file_transfer'; transfer_id: string; accepted: boolean }
  | {
    type: 'set_room_agent_mode';
    room_id: string;
    agent_mode?: NearbyRoomAgentMode;
    room_name?: string;
  }
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
  runAgentTurn?: (request: NearbyAgentTurnRequest, signal: AbortSignal) => Promise<string>;
  /** 本机主人可关闭 Agent 自动回复；缺省视为开启。 */
  autoReplyEnabled?: () => boolean;
}

export interface NearbyAgentHistoryEntry {
  sender: string;
  text: string;
}

export interface NearbyAgentTurnRequest {
  peerId: string;
  peerName: string;
  requestId: string;
  text: string;
  /** 群聊触发时携带；存在即后端按房间维度隔离会话。 */
  roomId?: string;
  roomName?: string;
  history?: NearbyAgentHistoryEntry[];
  /** 本机主人配置的工具白名单；空数组 = 全禁。 */
  allowedToolsets?: string[];
}

export interface NearbyPublishedAgent {
  public_agent_id: string;
  display_name: string;
  description?: string;
  kind?: string;
  capabilities?: string[];
  revision?: number;
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
  private localPeerId = '';
  private readonly peers = new Map<string, string>();
  private publishedAgents: NearbyPublishedAgent[] = [];

  public constructor(private readonly options: NearbyServiceOptions) {}

  public setPublishedAgents(agents: NearbyPublishedAgent[]): void {
    this.publishedAgents = agents
      .filter((agent) => agent.public_agent_id.trim() && agent.display_name.trim())
      .map((agent) => ({
        ...agent,
        public_agent_id: agent.public_agent_id.trim(),
        display_name: agent.display_name.trim(),
        capabilities: Array.isArray(agent.capabilities) ? [...new Set(agent.capabilities)] : [],
        revision: Math.max(1, Math.trunc(agent.revision ?? 1)),
      }));
  }

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
      if (event.type === 'ready') {
        const peer = this.eventPeer(event);
        if (peer) {
          this.localPeerId = peer.peerId;
          this.peers.set(peer.peerId, peer.displayName);
        }
        onReady();
      } else if (event.type === 'peer_discovered' || event.type === 'peer_connected') {
        const peer = this.eventPeer(event);
        if (peer) this.peers.set(peer.peerId, peer.displayName);
      }
      this.options.onEvent(event);
      this.maybeRunAgent(event);
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

  private eventPeer(event: NearbyEvent): { peerId: string; displayName: string } | null {
    const value = event.peer;
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const peer = value as Record<string, unknown>;
    const peerId = typeof peer.peer_id === 'string' ? peer.peer_id : '';
    if (!peerId) return null;
    return {
      peerId,
      displayName: typeof peer.display_name === 'string' && peer.display_name.trim()
        ? peer.display_name.trim()
        : '附近的用户',
    };
  }

  private maybeRunAgent(event: NearbyEvent): void {
    if (event.type !== 'message' || !this.options.runAgentTurn) return;
    const peerId = typeof event.peer_id === 'string' ? event.peer_id : '';
    const value = event.message;
    if (!peerId || !value || typeof value !== 'object' || Array.isArray(value)) return;
    const message = value as Record<string, unknown>;
    if (message.type !== 'agent.request' || message.sender === this.localPeerId) return;
    const requestId = typeof message.message_id === 'string' ? message.message_id : '';
    const payload = message.payload;
    if (!requestId || !payload || typeof payload !== 'object' || Array.isArray(payload)) return;
    void this.sendAgentReply(
      peerId,
      requestId,
      'Agent 不能参与私聊，请把 Agent 邀请到主人所在的群聊中协作',
      true,
    );
  }

  private async sendAgentReply(
    peerId: string,
    requestId: string,
    rawText: string,
    error: boolean,
  ): Promise<void> {
    const text = Array.from(String(rawText || '').trim()).slice(0, 8_000).join('');
    if (!text || !this.ready || !this.child?.stdin.writable) return;
    const command: NearbyCommand = {
      type: 'send_agent_reply',
      peer_id: peerId,
      request_id: requestId,
      text,
      error,
    };
    console.warn(`[nearby][agent] reply_queued peer=${peerId} request=${requestId} error=${error}`);
    this.child.stdin.write(`${JSON.stringify(command)}\n`);
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
    const runtimeArgs = [
      '--ipc',
      ...this.publishedAgents.flatMap((agent) => ['--published-agent', JSON.stringify(agent)]),
    ];
    if (binary) return { command: binary, args: runtimeArgs, cwd: path.dirname(binary) };
    if (this.options.isPackaged) {
      throw new Error(`找不到 Nearby 运行时：${candidates.join(', ')}`);
    }
    return {
      command: process.platform === 'win32' ? 'cargo.exe' : 'cargo',
      args: [
        'run', '--quiet', '--manifest-path', path.join(this.options.repoRoot, 'nearby', 'Cargo.toml'), '--',
        ...runtimeArgs,
      ],
      cwd: this.options.repoRoot,
    };
  }
}
