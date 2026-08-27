/**
 * 同伴页状态中心：peers / 会话（直聊 + 群聊统一模型）/ 消息 / 未读 / Agent 设置。
 *
 * 纯状态模块，不触碰 DOM，便于单测。事件来源是主进程转发的 Nearby 运行时
 * IPC 事件（nearby/src/ipc.rs 的 IpcEvent），消息体是 protocol.rs 的 Message
 * JSON（{ version, type, message_id, sender, payload }）。
 */

export type NearbyConnectionState = 'discovered' | 'connecting' | 'connected' | 'disconnected' | 'unavailable';

export type NearbyAgentMode = 'mention' | 'auto' | 'quiet';

export const AGENT_MODE_LABELS: Record<NearbyAgentMode, string> = {
  mention: '@触发',
  auto: '全员响应',
  quiet: '安静模式',
};

export interface NearbyPeer {
  peer_id: string;
  display_name: string;
  agent_name: string;
  capabilities: string[];
  published_agents: NearbyPublishedAgent[];
  connection: NearbyConnectionState;
}

export interface NearbyPublishedAgent {
  public_agent_id: string;
  display_name: string;
  source_kind: string;
  source_ref: string;
  description?: string;
}

export interface NearbyFileCard {
  file_id: string;
  name: string;
  mime_type: string;
  size: number;
  sha256: string;
  /** 完整文件才有内容；不完整的历史文件为 ''。 */
  data_base64: string;
  complete: boolean;
}

export type NearbyChatMessageKind = 'text' | 'agent' | 'file' | 'system';

export interface NearbyChatMessage {
  id: string;
  kind: NearbyChatMessageKind;
  senderPeerId: string;
  text: string;
  /** 毫秒时间戳；历史消息协议不带时间，记 0。 */
  timestamp: number;
  isOwn: boolean;
  isError: boolean;
  file?: NearbyFileCard;
}

export interface NearbyConversation {
  id: string;
  kind: 'dm' | 'room';
  title: string;
  /** 直聊会话的对端 peer_id；群聊为 ''。 */
  peerId: string;
  /** 群聊会话的 room_id；直聊为 ''。 */
  roomId: string;
  agentMode: NearbyAgentMode;
  memberIds: string[];
  /** 群主 peer_id；空串表示未知（直聊会话恒为 ''）。 */
  ownerPeerId: string;
  isOwner: boolean;
  unread: number;
  lastMessageText: string;
  lastMessageAt: number;
}

export interface NearbyAgentSettings {
  autoReply: boolean;
  allowedToolsets: string[];
}

export interface NearbyStoreNote {
  text: string;
  tone: 'normal' | 'error';
}

interface FileBuffer {
  conversationId: string;
  senderPeerId: string;
  isOwn: boolean;
  timestamp: number;
  fileId: string;
  name: string;
  mimeType: string;
  size: number;
  sha256: string;
  total: number;
  chunks: Map<number, string>;
}

export function dmConversationId(peerId: string): string {
  return `dm:${peerId}`;
}

export function roomConversationId(roomId: string): string {
  return `room:${roomId}`;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function asPublishedAgents(value: unknown): NearbyPublishedAgent[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const record = asRecord(item);
    const publicAgentId = asString(record?.public_agent_id);
    const displayName = asString(record?.display_name);
    if (!record || !publicAgentId || !displayName) return [];
    return [{
      public_agent_id: publicAgentId,
      display_name: displayName,
      source_kind: asString(record.source_kind, 'local'),
      source_ref: asString(record.source_ref),
      ...(asString(record.description) ? { description: asString(record.description) } : {}),
    }];
  });
}

function asPeer(value: unknown): Omit<NearbyPeer, 'connection'> | null {
  const record = asRecord(value);
  if (!record) return null;
  const peerId = asString(record.peer_id);
  if (!peerId) return null;
  return {
    peer_id: peerId,
    display_name: asString(record.display_name),
    agent_name: asString(record.agent_name),
    capabilities: asStringArray(record.capabilities),
    published_agents: asPublishedAgents(record.published_agents),
  };
}

function isAgentMode(value: unknown): value is NearbyAgentMode {
  return value === 'mention' || value === 'auto' || value === 'quiet';
}

export class NearbyStore {
  localPeerId = '';
  localName = '我';
  localAgentName = 'Ace Agent';
  discoverable = true;
  discovering = true;
  settings: NearbyAgentSettings = { autoReply: true, allowedToolsets: [] };
  activeConversationId: string | null = null;
  readonly peers = new Map<string, NearbyPeer>();
  readonly conversations = new Map<string, NearbyConversation>();
  readonly messages = new Map<string, NearbyChatMessage[]>();

  private readonly listeners = new Set<() => void>();
  private readonly pendingAgent = new Map<string, Set<string>>();
  private readonly fileBuffers = new Map<string, FileBuffer>();
  private systemSeq = 0;

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private changed(): void {
    for (const listener of this.listeners) listener();
  }

  peerLabel(peerId: string): string {
    if (peerId === this.localPeerId) return this.localName;
    const peer = this.peers.get(peerId);
    return peer?.display_name.trim() || peer?.agent_name.trim() || (peerId ? `同伴 ${peerId.slice(0, 6)}` : '同伴');
  }

  peerAgentLabel(peerId: string): string {
    if (peerId === this.localPeerId) return this.localAgentName || 'Ace Agent';
    const agentName = this.peers.get(peerId)?.agent_name.trim();
    return agentName || 'Ace Agent';
  }

  conversationMessages(conversationId: string): NearbyChatMessage[] {
    return this.messages.get(conversationId) ?? [];
  }

  isConversationOnline(conversation: NearbyConversation): boolean {
    if (conversation.kind === 'dm') {
      return this.peers.get(conversation.peerId)?.connection === 'connected';
    }
    return conversation.memberIds.some((peerId) => (
      peerId !== this.localPeerId && this.peers.get(peerId)?.connection === 'connected'
    ));
  }

  /** 正在等待哪些成员的 Agent 回复（渲染「思考中…」占位）。 */
  pendingAgentSenders(conversationId: string): string[] {
    return [...(this.pendingAgent.get(conversationId) ?? [])];
  }

  expectAgentReply(conversationId: string, peerIds: string[]): void {
    if (peerIds.length === 0) return;
    let pending = this.pendingAgent.get(conversationId);
    if (!pending) {
      pending = new Set();
      this.pendingAgent.set(conversationId, pending);
    }
    for (const peerId of peerIds) pending.add(peerId);
    this.changed();
  }

  private clearPending(conversationId: string, peerId: string): boolean {
    const pending = this.pendingAgent.get(conversationId);
    if (!pending?.delete(peerId)) return false;
    if (pending.size === 0) this.pendingAgent.delete(conversationId);
    return true;
  }

  setActiveConversation(conversationId: string | null): void {
    this.activeConversationId = conversationId;
    const conversation = conversationId ? this.conversations.get(conversationId) : undefined;
    if (conversation && conversation.unread > 0) conversation.unread = 0;
    this.changed();
  }

  applySettings(settings: NearbyAgentSettings): void {
    this.settings = settings;
    this.changed();
  }

  private ensureDmConversation(peerId: string): NearbyConversation {
    const id = dmConversationId(peerId);
    let conversation = this.conversations.get(id);
    if (!conversation) {
      conversation = {
        id,
        kind: 'dm',
        title: this.peerLabel(peerId),
        peerId,
        roomId: '',
        agentMode: 'mention',
        memberIds: [peerId],
        ownerPeerId: '',
        isOwner: false,
        unread: 0,
        lastMessageText: '',
        lastMessageAt: 0,
      };
      this.conversations.set(id, conversation);
    }
    return conversation;
  }

  private applyOwner(conversation: NearbyConversation, ownerPeerId: string | null | undefined): void {
    if (typeof ownerPeerId !== 'string' || !ownerPeerId) return;
    conversation.ownerPeerId = ownerPeerId;
    conversation.isOwner = ownerPeerId === this.localPeerId;
  }

  private upsertRoom(
    roomId: string,
    fields: { name?: string; agentMode?: NearbyAgentMode; memberIds?: string[]; ownerPeerId?: string | null },
  ): NearbyConversation {
    const id = roomConversationId(roomId);
    let conversation = this.conversations.get(id);
    if (!conversation) {
      conversation = {
        id,
        kind: 'room',
        title: fields.name?.trim() || '同伴群聊',
        peerId: '',
        roomId,
        agentMode: fields.agentMode ?? 'mention',
        memberIds: fields.memberIds ?? [],
        ownerPeerId: '',
        isOwner: false,
        unread: 0,
        lastMessageText: '',
        lastMessageAt: 0,
      };
      this.applyOwner(conversation, fields.ownerPeerId);
      this.conversations.set(id, conversation);
      return conversation;
    }
    if (fields.name?.trim()) conversation.title = fields.name.trim();
    if (fields.agentMode) conversation.agentMode = fields.agentMode;
    if (fields.memberIds) {
      conversation.memberIds = [...new Set([...fields.memberIds, ...conversation.memberIds])];
    }
    this.applyOwner(conversation, fields.ownerPeerId);
    return conversation;
  }

  private appendMessage(
    conversation: NearbyConversation,
    message: NearbyChatMessage,
    options: { countUnread: boolean },
  ): void {
    const list = this.messages.get(conversation.id) ?? [];
    if (list.some((item) => item.id === message.id)) return;
    list.push(message);
    this.messages.set(conversation.id, list);
    if (message.kind !== 'system' || !conversation.lastMessageText) {
      conversation.lastMessageText = message.kind === 'file'
        ? `[文件] ${message.file?.name ?? ''}`
        : message.text;
    }
    if (message.timestamp >= conversation.lastMessageAt) {
      conversation.lastMessageAt = message.timestamp;
    }
    if (options.countUnread && !message.isOwn && conversation.id !== this.activeConversationId) {
      conversation.unread += 1;
    }
  }

  private appendSystemMessage(conversation: NearbyConversation, text: string): void {
    this.systemSeq += 1;
    this.appendMessage(conversation, {
      id: `sys:${this.systemSeq}`,
      kind: 'system',
      senderPeerId: '',
      text,
      timestamp: Date.now(),
      isOwn: false,
      isError: false,
    }, { countUnread: false });
  }

  /** peer.file / room.file 分片按 file_id 重组，收齐后落成一条文件消息。 */
  private ingestFileChunk(raw: unknown, history: boolean, directPeerId = ''): void {
    const message = asRecord(raw);
    const payload = asRecord(message?.payload);
    const file = asRecord(payload?.file);
    const roomId = asString(payload?.room_id);
    if (!message || !file || (!roomId && !directPeerId)) return;
    const fileId = asString(file.file_id);
    const chunkIndex = typeof file.chunk_index === 'number' ? file.chunk_index : -1;
    const chunkTotal = typeof file.chunk_total === 'number' ? file.chunk_total : 0;
    if (!fileId || chunkIndex < 0 || chunkTotal <= 0) return;
    const conversationId = roomId
      ? roomConversationId(roomId)
      : this.ensureDmConversation(directPeerId).id;
    const conversation = this.conversations.get(conversationId);
    if (!conversation) return;
    const key = `${conversationId}:${fileId}`;
    let buffer = this.fileBuffers.get(key);
    if (!buffer) {
      const senderPeerId = asString(message.sender);
      buffer = {
        conversationId,
        senderPeerId,
        isOwn: senderPeerId === this.localPeerId,
        timestamp: history ? 0 : Date.now(),
        fileId,
        name: asString(file.name, '未命名文件'),
        mimeType: asString(file.mime_type, 'application/octet-stream'),
        size: typeof file.size === 'number' ? file.size : 0,
        sha256: asString(file.sha256),
        total: chunkTotal,
        chunks: new Map(),
      };
      this.fileBuffers.set(key, buffer);
    }
    buffer.chunks.set(chunkIndex, asString(file.data_base64));
    if (buffer.chunks.size < buffer.total) return;
    const parts: string[] = [];
    for (let index = 0; index < buffer.total; index += 1) {
      const part = buffer.chunks.get(index);
      if (part === undefined) return;
      parts.push(part);
    }
    this.fileBuffers.delete(key);
    this.appendMessage(conversation, {
      id: `file:${fileId}`,
      kind: 'file',
      senderPeerId: buffer.senderPeerId,
      text: buffer.name,
      timestamp: buffer.timestamp,
      isOwn: buffer.isOwn,
      isError: false,
      file: {
        file_id: buffer.fileId,
        name: buffer.name,
        mime_type: buffer.mimeType,
        size: buffer.size,
        sha256: buffer.sha256,
        data_base64: parts.join(''),
        complete: true,
      },
    }, { countUnread: !history });
  }

  /** 历史快照里没收齐的分片落成「不完整」文件卡片，避免静默丢消息。 */
  private flushIncompleteFiles(): void {
    for (const [key, buffer] of this.fileBuffers) {
      const conversation = this.conversations.get(buffer.conversationId);
      if (!conversation) continue;
      this.appendMessage(conversation, {
        id: `file:${buffer.fileId}`,
        kind: 'file',
        senderPeerId: buffer.senderPeerId,
        text: buffer.name,
        timestamp: buffer.timestamp,
        isOwn: buffer.isOwn,
        isError: false,
        file: {
          file_id: buffer.fileId,
          name: buffer.name,
          mime_type: buffer.mimeType,
          size: buffer.size,
          sha256: buffer.sha256,
          data_base64: '',
          complete: false,
        },
      }, { countUnread: false });
      this.fileBuffers.delete(key);
    }
  }

  private ingestDmMessage(peerId: string, raw: unknown, history: boolean): void {
    const message = asRecord(raw);
    if (!message) return;
    const type = asString(message.type);
    if (!['agent.request', 'agent.response', 'agent.error', 'peer.message'].includes(type)) return;
    const payload = asRecord(message.payload);
    const text = asString(payload?.text).trim();
    if (!text) return;
    const conversation = this.ensureDmConversation(peerId);
    const senderPeerId = asString(message.sender);
    const isOwn = senderPeerId === this.localPeerId;
    const fromAgent = type === 'agent.response' || type === 'agent.error';
    if (!isOwn && fromAgent) this.clearPending(conversation.id, peerId);
    this.appendMessage(conversation, {
      id: asString(message.message_id, `dm:${peerId}:${conversation.lastMessageAt}:${text.length}`),
      kind: fromAgent ? 'agent' : 'text',
      senderPeerId,
      text,
      timestamp: history ? 0 : Date.now(),
      isOwn,
      isError: type === 'agent.error',
    }, { countUnread: !history });
  }

  private ingestRoomMessage(raw: unknown, history: boolean): void {
    const message = asRecord(raw);
    const payload = asRecord(message?.payload);
    const roomId = asString(payload?.room_id);
    if (!message || !payload || !roomId) return;
    const conversation = this.conversations.get(roomConversationId(roomId));
    if (!conversation) return;
    const senderPeerId = asString(message.sender);
    const text = asString(payload.text).trim();
    if (!text) return;
    const isOwn = senderPeerId === this.localPeerId;
    // 群内 Agent 回复在协议层与普通消息同型；用「发出时登记的期待回复」区分：
    // 期待中的成员下一条消息按 Agent 气泡渲染。
    const isAgentReply = !isOwn && this.clearPending(conversation.id, senderPeerId);
    if (!conversation.memberIds.includes(senderPeerId) && senderPeerId) {
      conversation.memberIds = [...conversation.memberIds, senderPeerId];
    }
    this.appendMessage(conversation, {
      id: asString(message.message_id, `room:${roomId}:${Date.now()}`),
      kind: isAgentReply ? 'agent' : 'text',
      senderPeerId,
      text,
      timestamp: history ? 0 : Date.now(),
      isOwn,
      isError: false,
    }, { countUnread: !history });
  }

  private ingestProtocolMessage(peerId: string, raw: unknown, history: boolean): void {
    const type = asString(asRecord(raw)?.type);
    if (type === 'room.message') this.ingestRoomMessage(raw, history);
    else if (type === 'room.file') this.ingestFileChunk(raw, history);
    else if (type === 'peer.file') this.ingestFileChunk(raw, history, peerId);
    else this.ingestDmMessage(peerId, raw, history);
  }

  private hydrate(snapshot: Record<string, unknown>): void {
    for (const rawRoom of Array.isArray(snapshot.rooms) ? snapshot.rooms : []) {
      const room = asRecord(rawRoom);
      if (!room) continue;
      const roomId = asString(room.room_id);
      if (!roomId) continue;
      this.upsertRoom(roomId, {
        name: asString(room.room_name),
        agentMode: isAgentMode(room.agent_mode) ? room.agent_mode : 'mention',
        memberIds: asStringArray(room.peer_ids),
        ownerPeerId: asString(room.owner_peer_id) || null,
      });
      const messages = Array.isArray(room.messages) ? room.messages : [];
      for (const raw of messages) this.ingestProtocolMessage(asString(asRecord(raw)?.sender), raw, true);
    }
    for (const rawDm of Array.isArray(snapshot.dms) ? snapshot.dms : []) {
      const dm = asRecord(rawDm);
      if (!dm) continue;
      const peerId = asString(dm.peer_id);
      if (!peerId) continue;
      this.ensureDmConversation(peerId);
      const messages = Array.isArray(dm.messages) ? dm.messages : [];
      for (const raw of messages) this.ingestProtocolMessage(peerId, raw, true);
    }
    this.flushIncompleteFiles();
  }

  /**
   * 应用一条 Nearby 运行时事件；返回用于页面状态条的一句话提示（无提示返回 null）。
   */
  applyEvent(event: { type: string; [key: string]: unknown }): NearbyStoreNote | null {
    let note: NearbyStoreNote | null = null;
    switch (event.type) {
      case 'ready': {
        const peer = asPeer(event.peer);
        if (peer) {
          this.localPeerId = peer.peer_id;
          this.localName = peer.display_name.trim() || '我';
          this.localAgentName = peer.agent_name.trim() || 'Ace Agent';
          // 快照可能先于 ready 到达：拿到本机 peer_id 后重算群主身份
          for (const conversation of this.conversations.values()) {
            if (conversation.kind === 'room' && conversation.ownerPeerId) {
              conversation.isOwner = conversation.ownerPeerId === this.localPeerId;
            }
          }
        }
        this.discoverable = event.discoverable !== false;
        break;
      }
      case 'discovery_started':
        this.discovering = true;
        note = { text: '正在查找附近 Ace', tone: 'normal' };
        break;
      case 'discovery_stopped':
        this.discovering = false;
        note = { text: '查找已暂停', tone: 'normal' };
        break;
      case 'discoverability_changed':
        this.discoverable = event.discoverable !== false;
        note = {
          text: this.discoverable ? '附近设备可以发现你' : '已对附近设备隐藏',
          tone: 'normal',
        };
        break;
      case 'peer_discovered': {
        const peer = asPeer(event.peer);
        if (!peer || peer.peer_id === this.localPeerId) break;
        const current = this.peers.get(peer.peer_id);
        this.peers.set(peer.peer_id, {
          ...peer,
          connection: current && current.connection !== 'unavailable' && current.connection !== 'disconnected'
            ? current.connection
            : 'discovered',
        });
        this.refreshDmTitle(peer.peer_id);
        break;
      }
      case 'peer_connected': {
        const peer = asPeer(event.peer);
        if (!peer) break;
        const wasConnecting = this.peers.get(peer.peer_id)?.connection === 'connecting';
        this.peers.set(peer.peer_id, { ...peer, connection: 'connected' });
        const conversation = this.ensureDmConversation(peer.peer_id);
        this.refreshDmTitle(peer.peer_id);
        // 本机主动发起的连接：直接点亮会话；被动接入只生成/更新会话，不打断当前操作。
        if (wasConnecting || !this.activeConversationId) {
          this.setActiveConversation(conversation.id);
        }
        note = { text: `已连接 ${this.peerLabel(peer.peer_id)}`, tone: 'normal' };
        break;
      }
      case 'peer_disconnected': {
        const peerId = asString(event.peer_id);
        const peer = this.peers.get(peerId);
        if (peer) this.peers.set(peerId, { ...peer, connection: 'disconnected' });
        if (peerId) this.clearPending(dmConversationId(peerId), peerId);
        note = { text: peer ? `与 ${this.peerLabel(peerId)} 的连接已断开` : '连接已断开', tone: 'normal' };
        break;
      }
      case 'peer_unavailable': {
        const peerId = asString(event.peer_id);
        const peer = this.peers.get(peerId);
        if (peer && peer.connection !== 'connected') {
          this.peers.set(peerId, { ...peer, connection: 'unavailable' });
        }
        break;
      }
      case 'peer_connection_failed': {
        const peerId = asString(event.peer_id);
        const peer = this.peers.get(peerId);
        if (peer && peer.connection !== 'connected') {
          this.peers.set(peerId, { ...peer, connection: 'discovered' });
        }
        note = { text: asString(event.message, '连接失败'), tone: 'error' };
        break;
      }
      case 'peer_message_received': {
        const peerId = asString(event.peer_id);
        const text = asString(event.text).trim();
        if (!peerId || !text) break;
        const conversation = this.ensureDmConversation(peerId);
        this.appendMessage(conversation, {
          id: asString(event.message_id, `pm:${peerId}:${Date.now()}`),
          kind: 'text',
          senderPeerId: peerId,
          text,
          timestamp: typeof event.timestamp === 'number' ? event.timestamp * 1000 : Date.now(),
          isOwn: false,
          isError: false,
        }, { countUnread: true });
        if (!this.activeConversationId) this.setActiveConversation(conversation.id);
        note = { text: `收到 ${asString(event.display_name) || this.peerLabel(peerId)} 的消息`, tone: 'normal' };
        break;
      }
      case 'message': {
        const peerId = asString(event.peer_id);
        if (peerId) this.ingestProtocolMessage(peerId, event.message, false);
        break;
      }
      case 'room_created': {
        const roomId = asString(event.room_id);
        if (!roomId) break;
        const name = asString(event.room_name, '同伴群聊');
        const agentMode = isAgentMode(event.agent_mode) ? event.agent_mode : 'mention';
        const conversation = this.upsertRoom(roomId, {
          name,
          agentMode,
          memberIds: asStringArray(event.peer_ids),
          ownerPeerId: asString(event.owner_peer_id) || null,
        });
        this.appendSystemMessage(conversation, `你创建了群聊，Agent 触发模式：${AGENT_MODE_LABELS[agentMode]}`);
        this.setActiveConversation(conversation.id);
        note = { text: `已创建群聊「${name}」`, tone: 'normal' };
        break;
      }
      case 'room_joined': {
        const roomId = asString(event.room_id);
        if (!roomId) break;
        const name = asString(event.room_name, '同伴群聊');
        const agentMode = isAgentMode(event.agent_mode) ? event.agent_mode : 'mention';
        const conversation = this.upsertRoom(roomId, {
          name,
          agentMode,
          memberIds: asStringArray(event.peer_ids),
          ownerPeerId: asString(event.owner_peer_id) || null,
        });
        this.appendSystemMessage(conversation, `你加入了群聊，Agent 触发模式：${AGENT_MODE_LABELS[agentMode]}`);
        this.setActiveConversation(conversation.id);
        note = { text: `已加入群聊「${name}」`, tone: 'normal' };
        break;
      }
      case 'room_restored': {
        const roomId = asString(event.room_id);
        if (!roomId) break;
        this.upsertRoom(roomId, {
          name: asString(event.room_name),
          ...(isAgentMode(event.agent_mode) ? { agentMode: event.agent_mode } : {}),
          memberIds: asStringArray(event.peer_ids),
          ownerPeerId: asString(event.owner_peer_id) || null,
        });
        const messages = Array.isArray(event.messages) ? event.messages : [];
        for (const raw of messages) this.ingestProtocolMessage(asString(asRecord(raw)?.sender), raw, true);
        break;
      }
      case 'room_settings_updated': {
        const roomId = asString(event.room_id);
        if (!roomId) break;
        const conversation = this.conversations.get(roomConversationId(roomId));
        if (!conversation) break;
        const roomName = asString(event.room_name).trim();
        if (roomName && roomName !== conversation.title) {
          conversation.title = roomName;
          this.appendSystemMessage(conversation, `群主修改了群名为「${roomName}」`);
          note = { text: `群聊已更名为「${roomName}」`, tone: 'normal' };
        }
        const agentMode = isAgentMode(event.agent_mode) ? event.agent_mode : null;
        if (agentMode && conversation.agentMode !== agentMode) {
          conversation.agentMode = agentMode;
          this.appendSystemMessage(conversation, `Agent 触发模式已切换为「${AGENT_MODE_LABELS[agentMode]}」`);
          note = { text: `群聊「${conversation.title}」的 Agent 触发模式已更新`, tone: 'normal' };
        }
        break;
      }
      case 'room_member_joined':
      case 'room_member_left': {
        const roomId = asString(event.room_id);
        const peerId = asString(event.peer_id);
        if (!roomId || !peerId) break;
        const conversation = this.conversations.get(roomConversationId(roomId));
        if (!conversation) break;
        const name = asString(event.display_name).trim() || peerId;
        if (event.type === 'room_member_joined') {
          if (!conversation.memberIds.includes(peerId)) {
            conversation.memberIds = [...conversation.memberIds, peerId];
          }
          this.appendSystemMessage(conversation, `${name} 加入了群聊`);
        } else {
          conversation.memberIds = conversation.memberIds.filter((memberId) => memberId !== peerId);
          this.clearPending(conversation.id, peerId);
          this.appendSystemMessage(conversation, `${name} 退出了群聊`);
        }
        break;
      }
      case 'room_left': {
        const roomId = asString(event.room_id);
        if (!roomId) break;
        const conversationId = roomConversationId(roomId);
        const title = this.conversations.get(conversationId)?.title ?? '';
        this.conversations.delete(conversationId);
        this.messages.delete(conversationId);
        this.pendingAgent.delete(conversationId);
        if (this.activeConversationId === conversationId) this.activeConversationId = null;
        note = { text: title ? `已退出群聊「${title}」` : '已退出群聊', tone: 'normal' };
        break;
      }
      case 'history_snapshot':
        this.hydrate(event);
        break;
      case 'error':
        note = { text: asString(event.message, '同伴服务发生错误'), tone: 'error' };
        break;
      default:
        return null;
    }
    this.changed();
    return note;
  }

  private refreshDmTitle(peerId: string): void {
    const conversation = this.conversations.get(dmConversationId(peerId));
    if (conversation) conversation.title = this.peerLabel(peerId);
  }
}
