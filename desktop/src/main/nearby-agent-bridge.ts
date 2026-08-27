import * as fs from 'fs';
import * as path from 'path';

import type {
  NearbyAgentHistoryEntry,
  NearbyAgentTurnRequest,
  NearbyCommand,
  NearbyEvent,
  NearbyRoomAgentMode,
} from './nearby-service';

const MAX_CONCURRENT_AGENT_TURNS = 8;
const ROOM_CONTEXT_HISTORY_LIMIT = 20;
const MAX_AGENT_REPLY_CHARS = 8_000;

export interface NearbyAgentSettings {
  autoReply: boolean;
  allowedToolsets: string[];
}

export interface NearbyAgentBridgeOptions {
  sendCommand: (command: NearbyCommand) => void;
  runAgentTurn?: (request: NearbyAgentTurnRequest, signal: AbortSignal) => Promise<string>;
  getSettings: () => NearbyAgentSettings;
}

interface NearbyRoomState {
  name: string;
  agentMode: NearbyRoomAgentMode;
  history: NearbyAgentHistoryEntry[];
}

function nearbySettingsPath(crewHome: string): string {
  return path.join(crewHome, 'nearby', 'settings.json');
}

function normalizeToolsets(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const toolsets = raw
    .filter((value): value is string => typeof value === 'string' && value.trim().length > 0)
    .map((value) => value.trim());
  return [...new Set(toolsets)];
}

function readSettingsFile(crewHome: string): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(fs.readFileSync(nearbySettingsPath(crewHome), 'utf8'));
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // 文件不存在或内容损坏时按空配置处理
  }
  return {};
}

export function loadNearbyAgentSettings(crewHome: string): NearbyAgentSettings {
  const record = readSettingsFile(crewHome);
  return {
    autoReply: typeof record.auto_reply === 'boolean' ? record.auto_reply : true,
    allowedToolsets: normalizeToolsets(record.allowed_toolsets),
  };
}

/**
 * 合并写入同伴 Agent 设置。
 *
 * settings.json 同时被 Nearby 运行时持有（discoverable 等字段），这里读改写合并，
 * 只覆盖 auto_reply / allowed_toolsets，其余字段原样保留。
 */
export function saveNearbyAgentSettings(
  crewHome: string,
  patch: Partial<NearbyAgentSettings>,
): NearbyAgentSettings {
  const next: Record<string, unknown> = { ...readSettingsFile(crewHome) };
  if (patch.autoReply !== undefined) next.auto_reply = patch.autoReply;
  if (patch.allowedToolsets !== undefined) next.allowed_toolsets = normalizeToolsets(patch.allowedToolsets);
  const filePath = nearbySettingsPath(crewHome);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(next, null, 2)}\n`, 'utf8');
  return {
    autoReply: typeof next.auto_reply === 'boolean' ? next.auto_reply : true,
    allowedToolsets: normalizeToolsets(next.allowed_toolsets),
  };
}

/**
 * 群内 Agent 编排器。
 *
 * 监听 Nearby 运行时事件流，维护房间 agent_mode 与最近消息，按触发模式裁决
 * 本机 Agent 是否响应；裁决通过后走 runAgentTurn，回复以 send_room_message
 * 发回群内并 @ 原发送者。并发上限、消息去重、回复截断与 1:1 通道保持一致。
 */
export class NearbyAgentBridge {
  private localPeerId = '';
  private localDisplayName = '';
  private readonly peerNames = new Map<string, string>();
  private readonly rooms = new Map<string, NearbyRoomState>();
  private readonly agentRuns = new Map<string, AbortController>();

  public constructor(private readonly options: NearbyAgentBridgeOptions) {}

  public handleEvent(event: NearbyEvent): void {
    switch (event.type) {
      case 'ready':
      case 'peer_discovered':
      case 'peer_connected':
        this.rememberPeer(event);
        break;
      case 'history_snapshot':
        this.applyHistorySnapshot(event);
        break;
      case 'room_created':
      case 'room_joined':
      case 'room_restored':
        this.applyRoomMetadata(event);
        break;
      case 'room_settings_updated': {
        const roomId = typeof event.room_id === 'string' ? event.room_id : '';
        if (!roomId) break;
        const room = this.roomState(roomId);
        if (this.isAgentMode(event.agent_mode)) room.agentMode = event.agent_mode;
        if (typeof event.room_name === 'string' && event.room_name.trim()) {
          room.name = event.room_name.trim();
        }
        break;
      }
      case 'room_left': {
        const roomId = typeof event.room_id === 'string' ? event.room_id : '';
        if (roomId) this.rooms.delete(roomId);
        break;
      }
      case 'message':
        this.handleMessage(event);
        break;
      default:
        break;
    }
  }

  public dispose(): void {
    for (const controller of this.agentRuns.values()) controller.abort();
    this.agentRuns.clear();
  }

  private isAgentMode(value: unknown): value is NearbyRoomAgentMode {
    return value === 'mention' || value === 'auto' || value === 'quiet';
  }

  private rememberPeer(event: NearbyEvent): void {
    const value = event.peer;
    if (!value || typeof value !== 'object' || Array.isArray(value)) return;
    const peer = value as Record<string, unknown>;
    const peerId = typeof peer.peer_id === 'string' ? peer.peer_id : '';
    if (!peerId) return;
    const displayName = typeof peer.display_name === 'string' && peer.display_name.trim()
      ? peer.display_name.trim()
      : peerId;
    this.peerNames.set(peerId, displayName);
    if (event.type === 'ready') {
      this.localPeerId = peerId;
      this.localDisplayName = displayName;
    }
  }

  private peerName(peerId: string): string {
    if (peerId === this.localPeerId && this.localDisplayName) return this.localDisplayName;
    return this.peerNames.get(peerId) ?? peerId;
  }

  private roomState(roomId: string): NearbyRoomState {
    let room = this.rooms.get(roomId);
    if (!room) {
      room = { name: '', agentMode: 'mention', history: [] };
      this.rooms.set(roomId, room);
    }
    return room;
  }

  private applyRoomMetadata(event: NearbyEvent): void {
    const roomId = typeof event.room_id === 'string' ? event.room_id : '';
    if (!roomId) return;
    const room = this.roomState(roomId);
    if (typeof event.room_name === 'string' && event.room_name.trim()) {
      room.name = event.room_name.trim();
    }
    if (this.isAgentMode(event.agent_mode)) room.agentMode = event.agent_mode;
    if (Array.isArray(event.messages)) this.importRoomHistory(room, event.messages);
  }

  private applyHistorySnapshot(event: NearbyEvent): void {
    if (!Array.isArray(event.rooms)) return;
    for (const entry of event.rooms) {
      if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue;
      this.applyRoomMetadata(entry as NearbyEvent);
    }
  }

  private importRoomHistory(room: NearbyRoomState, messages: unknown[]): void {
    const entries: NearbyAgentHistoryEntry[] = [];
    for (const raw of messages) {
      const entry = this.roomHistoryEntry(raw);
      if (entry) entries.push(entry);
    }
    room.history = entries.slice(-ROOM_CONTEXT_HISTORY_LIMIT);
  }

  private roomHistoryEntry(raw: unknown): NearbyAgentHistoryEntry | null {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
    const message = raw as Record<string, unknown>;
    if (message.type !== 'room.message') return null;
    const payload = message.payload;
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    const text = (payload as Record<string, unknown>).text;
    if (typeof text !== 'string' || !text.trim()) return null;
    const sender = typeof message.sender === 'string' ? message.sender : '';
    return { sender: this.peerName(sender), text };
  }

  private handleMessage(event: NearbyEvent): void {
    const value = event.message;
    if (!value || typeof value !== 'object' || Array.isArray(value)) return;
    const message = value as Record<string, unknown>;
    if (message.type !== 'room.message') return;
    const payload = message.payload;
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return;
    const body = payload as Record<string, unknown>;
    const roomId = typeof body.room_id === 'string' ? body.room_id : '';
    const sender = typeof message.sender === 'string' ? message.sender : '';
    const text = typeof body.text === 'string' ? body.text.trim() : '';
    if (!roomId || !sender || !text) return;

    // history 传入触发消息之前的最近消息，触发消息本身作为 query 传给后端
    const room = this.roomState(roomId);
    const history = room.history.slice(-ROOM_CONTEXT_HISTORY_LIMIT);
    room.history.push({ sender: this.peerName(sender), text });
    if (room.history.length > ROOM_CONTEXT_HISTORY_LIMIT) {
      room.history.splice(0, room.history.length - ROOM_CONTEXT_HISTORY_LIMIT);
    }
    this.maybeRespond(roomId, room, sender, message, text, history);
  }

  private maybeRespond(
    roomId: string,
    room: NearbyRoomState,
    sender: string,
    message: Record<string, unknown>,
    text: string,
    history: NearbyAgentHistoryEntry[],
  ): void {
    if (!this.options.runAgentTurn) return;
    if (sender === this.localPeerId) return;
    const settings = this.options.getSettings();
    if (!settings.autoReply) return;
    if (room.agentMode === 'quiet') return;
    if (room.agentMode === 'mention') {
      const mentions = Array.isArray((message.payload as Record<string, unknown>).mentions)
        ? (message.payload as Record<string, unknown>).mentions as unknown[]
        : [];
      // 协议层 mentions 取值是 peer_id（发送前会按房间成员过滤）
      if (!this.localPeerId || !mentions.includes(this.localPeerId)) return;
    }

    const requestId = typeof message.message_id === 'string' ? message.message_id : '';
    if (!requestId) return;
    const runKey = `${roomId}\0${requestId}`;
    if (this.agentRuns.has(runKey)) return;
    if (this.agentRuns.size >= MAX_CONCURRENT_AGENT_TURNS) {
      this.sendRoomReply(roomId, sender, 'Agent 当前忙碌，请稍后再试');
      return;
    }

    const controller = new AbortController();
    this.agentRuns.set(runKey, controller);
    console.warn(`[nearby][agent] room_request_received room=${roomId} request=${requestId}`);
    const request: NearbyAgentTurnRequest = {
      peerId: sender,
      peerName: this.peerName(sender),
      requestId,
      text,
      roomId,
      roomName: room.name,
      history,
      allowedToolsets: settings.allowedToolsets,
    };
    void this.options.runAgentTurn(request, controller.signal)
      .then((reply) => {
        console.warn(`[nearby][agent] room_turn_completed room=${roomId} request=${requestId}`);
        this.sendRoomReply(roomId, sender, reply);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const detail = error instanceof Error ? error.message : String(error);
        console.warn(`[nearby][agent] room_turn_failed room=${roomId} request=${requestId} error=${detail}`);
        this.sendRoomReply(roomId, sender, detail || 'Agent 暂时无法回复');
      })
      .finally(() => this.agentRuns.delete(runKey));
  }

  private sendRoomReply(roomId: string, sender: string, rawText: string): void {
    const text = Array.from(String(rawText || '').trim()).slice(0, MAX_AGENT_REPLY_CHARS).join('');
    if (!text) return;
    const command: NearbyCommand = {
      type: 'send_room_message',
      room_id: roomId,
      text,
      mentions: [sender],
    };
    console.warn(`[nearby][agent] room_reply_queued room=${roomId} peer=${sender}`);
    this.options.sendCommand(command);
  }
}
