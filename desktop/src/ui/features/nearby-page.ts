/**
 * 同伴页（Nearby）页面壳：三栏布局 + Nearby 事件总线接线。
 *
 * - nearby-store.ts    状态中心（peers / 会话 / 消息 / 未读 / Agent 设置）
 * - nearby-sidebar.ts  左栏：身份卡、发现面板、会话列表、弹层
 * - nearby-chat.ts     主区聊天窗 + 群聊右侧成员/设置面板
 *
 * 本模块负责把 NearbyActions（指令出口）接到 window.Crew 的 preload 桥，
 * 并把 onNearbyEvent 事件流转交给 store，再把 store 的变更扇出到两个子面板。
 */

import { createNearbyChat, type NearbyChatPane } from './nearby-chat';
import { createNearbySidebar, type NearbySidebar } from './nearby-sidebar';
import {
  roomConversationId,
  NearbyStore,
  type NearbyAgentMode,
  type NearbyAgentSettings,
  type NearbyConversation,
  type NearbyFileCard,
} from './nearby-store';

export interface NearbyPage {
  activate(): void;
  dispose(): void;
}

export interface NearbyMention {
  peerId: string;
  kind: 'person' | 'agent';
  label: string;
}

export interface NearbyActions {
  connectPeer(peerId: string): void;
  disconnectPeer(peerId: string): void;
  toggleDiscovery(): void;
  setDiscoverable(enabled: boolean): void;
  selectConversation(conversationId: string): void;
  sendMessage(text: string, mentions: NearbyMention[]): void;
  sendFile(): void;
  createRoom(name: string, memberIds: string[], agentMode: NearbyAgentMode): void;
  inviteToRoom(roomId: string, peerIds: string[]): void;
  setRoomAgentMode(roomId: string, agentMode: NearbyAgentMode): void;
  renameRoom(roomId: string, name: string): void;
  leaveRoom(roomId: string): void;
  saveFile(file: NearbyFileCard): Promise<void>;
  getAgentSettings(): Promise<NearbyAgentSettings>;
  saveAgentSettings(patch: Partial<NearbyAgentSettings>): Promise<NearbyAgentSettings | null>;
  loadToolsets(): Promise<string[]>;
  showStatus(text: string, tone?: 'normal' | 'error'): void;
}

const MAX_NEARBY_FILE_BYTES = 4 * 1024 * 1024;

type NearbyCommandPayload = Parameters<NonNullable<Window['Crew']['nearbyCommand']>>[0];

function textElement<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  text: string,
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function newRoomId(): string {
  // 主进程校验规则：/^[A-Za-z0-9_.:-]{1,120}$/
  return `room_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function bluetoothHelpText(): string {
  switch (window.Crew?.runtimePlatform) {
    case 'darwin':
      return '请在「系统设置 → 隐私与安全性 → 蓝牙」中允许 Ace 使用蓝牙，并确认蓝牙已开启。';
    case 'win32':
      return '请在「设置 → 蓝牙和设备」中开启蓝牙，并允许 Ace 访问蓝牙。';
    case 'linux':
      return '请确认蓝牙服务已启动（例如 systemctl start bluetooth）且适配器可用。';
    default:
      return '请确认系统蓝牙已开启，并允许 Ace 使用蓝牙。';
  }
}

export function mountNearbyPage(root: HTMLElement, bridge: Window['Crew'] = window.Crew): NearbyPage {
  const store = new NearbyStore();

  const page = document.createElement('div');
  page.className = 'nearby-page';

  const header = document.createElement('header');
  header.className = 'nearby-page__header';
  const heading = document.createElement('div');
  heading.className = 'nearby-page__heading';
  heading.append(
    textElement('h1', 'nearby-page__title', '同伴'),
    textElement('p', 'nearby-page__subtitle', '通过蓝牙发现附近的 Ace，把人拉进一个群，人带着自己的 Agent 一起协同。'),
  );
  const status = textElement('span', 'nearby-page__status', '正在准备…');
  header.append(heading, status);

  const banner = document.createElement('div');
  banner.className = 'nearby-banner';
  banner.hidden = true;
  const bannerText = textElement('span', 'nearby-banner__text', '');
  const bannerDismiss = document.createElement('button');
  bannerDismiss.type = 'button';
  bannerDismiss.className = 'nearby-banner__dismiss';
  bannerDismiss.textContent = '知道了';
  bannerDismiss.addEventListener('click', () => {
    banner.hidden = true;
  });
  banner.append(bannerText, bannerDismiss);

  const workspace = document.createElement('div');
  workspace.className = 'nearby-workspace';
  page.append(header, banner, workspace);
  root.replaceChildren(page);

  const setStatus = (text: string, tone: 'normal' | 'error' = 'normal'): void => {
    status.textContent = text;
    status.dataset.tone = tone;
  };

  const showBluetoothBanner = (message: string): void => {
    bannerText.textContent = `蓝牙不可用或权限未授予（${message}）。${bluetoothHelpText()}`;
    banner.hidden = false;
  };

  const command = (payload: NearbyCommandPayload): void => {
    void Promise.resolve(bridge?.nearbyCommand?.(payload))
      .catch((error: unknown) => setStatus(`操作失败：${errorMessage(error)}`, 'error'));
  };

  const activeConversation = (): NearbyConversation | null => {
    const id = store.activeConversationId;
    return id ? store.conversations.get(id) ?? null : null;
  };

  // actions 的方法体只在用户交互时执行；panes 在 actions 定义后构造、使用前必已就绪。
  const panes: { sidebar?: NearbySidebar; chat?: NearbyChatPane } = {};

  const actions: NearbyActions = {
    connectPeer(peerId) {
      const peer = store.peers.get(peerId);
      if (!peer || peer.connection === 'connected' || peer.connection === 'connecting') return;
      store.peers.set(peerId, { ...peer, connection: 'connecting' });
      setStatus(`正在连接 ${store.peerLabel(peerId)}…`);
      command({ type: 'connect_peer', peer_id: peerId });
      renderAll();
    },
    disconnectPeer(peerId) {
      command({ type: 'disconnect_peer', peer_id: peerId });
    },
    toggleDiscovery() {
      command({ type: store.discovering ? 'stop_discovery' : 'start_discovery' });
    },
    setDiscoverable(enabled) {
      command({ type: 'set_discoverable', enabled });
    },
    selectConversation(conversationId) {
      if (!store.conversations.has(conversationId)) return;
      store.setActiveConversation(conversationId);
      panes.chat?.focusComposer();
    },
    sendMessage(text, mentions) {
      const conversation = activeConversation();
      if (!conversation) return;
      if (conversation.kind === 'dm') {
        const peerId = conversation.peerId;
        const agentMention = mentions.find((mention) => mention.kind === 'agent' && mention.peerId === peerId);
        if (agentMention) {
          command({ type: 'send_agent_request', peer_id: peerId, text });
          store.expectAgentReply(conversation.id, [peerId]);
        } else {
          command({
            type: 'send_peer_message',
            peer_id: peerId,
            text,
            mentions: mentions.map((mention) => mention.peerId),
          });
        }
        return;
      }
      const mentionIds = [...new Set(mentions.map((mention) => mention.peerId))];
      command({ type: 'send_room_message', room_id: conversation.roomId, text, mentions: mentionIds });
      // 「思考中…」占位的预期集合：@触发 = 被 @ 的成员；全员响应 = 除我以外的成员；安静模式 = 无
      if (conversation.agentMode === 'mention' && mentionIds.length > 0) {
        store.expectAgentReply(conversation.id, mentionIds);
      } else if (conversation.agentMode === 'auto') {
        store.expectAgentReply(
          conversation.id,
          conversation.memberIds.filter((memberId) => memberId !== store.localPeerId),
        );
      }
    },
    sendFile() {
      const conversation = activeConversation();
      if (!conversation || conversation.kind !== 'room') return;
      void Promise.resolve(bridge?.nearbySelectFile?.()).then((file) => {
        if (!file) return;
        if (file.size > MAX_NEARBY_FILE_BYTES) {
          setStatus('文件超过 4 MiB，无法通过同伴通道发送', 'error');
          return;
        }
        command({
          type: 'send_room_file',
          room_id: conversation.roomId,
          file_id: file.file_id,
          name: file.name,
          mime_type: file.mime_type,
          size: file.size,
          sha256: file.sha256,
          data_base64: file.data_base64,
        });
        setStatus(`正在发送文件「${file.name}」…`);
      }).catch((error: unknown) => setStatus(`读取文件失败：${errorMessage(error)}`, 'error'));
    },
    createRoom(name, memberIds, agentMode) {
      command({
        type: 'create_room',
        room_id: newRoomId(),
        room_name: name,
        peer_ids: memberIds,
        agent_mode: agentMode,
      });
    },
    inviteToRoom(roomId, peerIds) {
      if (!store.conversations.has(roomConversationId(roomId)) || peerIds.length === 0) return;
      command({ type: 'invite_to_room', room_id: roomId, peer_ids: [...new Set(peerIds)] });
    },
    setRoomAgentMode(roomId, agentMode) {
      command({ type: 'set_room_agent_mode', room_id: roomId, agent_mode: agentMode });
    },
    renameRoom(roomId, name) {
      const roomName = name.trim();
      if (!roomName) return;
      command({ type: 'set_room_agent_mode', room_id: roomId, room_name: roomName });
    },
    leaveRoom(roomId) {
      command({ type: 'leave_room', room_id: roomId });
    },
    async saveFile(file) {
      try {
        const result = await bridge?.nearbySaveFile?.({
          name: file.name,
          mime_type: file.mime_type,
          size: file.size,
          sha256: file.sha256,
          data_base64: file.data_base64,
        });
        if (result?.ok && result.path) setStatus(`已保存到 ${result.path}`);
        else if (result?.ok) setStatus('文件已保存');
      } catch (error) {
        setStatus(`保存失败：${errorMessage(error)}`, 'error');
      }
    },
    async getAgentSettings() {
      try {
        const result = await bridge?.nearbyGetSettings?.();
        if (result?.ok) {
          return {
            autoReply: result.auto_reply,
            allowedToolsets: Array.isArray(result.allowed_toolsets) ? result.allowed_toolsets : [],
          };
        }
      } catch {
        // 读取失败时回退到本地默认（自动回复开、白名单空）
      }
      return { autoReply: true, allowedToolsets: [] };
    },
    async saveAgentSettings(patch) {
      try {
        const result = await bridge?.nearbySetSettings?.({
          ...(patch.autoReply !== undefined ? { auto_reply: patch.autoReply } : {}),
          ...(patch.allowedToolsets !== undefined ? { allowed_toolsets: patch.allowedToolsets } : {}),
        });
        if (result?.ok) {
          const next: NearbyAgentSettings = {
            autoReply: result.auto_reply,
            allowedToolsets: Array.isArray(result.allowed_toolsets) ? result.allowed_toolsets : [],
          };
          store.applySettings(next);
          return next;
        }
      } catch (error) {
        setStatus(`保存 Agent 设置失败：${errorMessage(error)}`, 'error');
      }
      return null;
    },
    async loadToolsets() {
      try {
        const { backendApi } = await import('../backend-client');
        const toolsets = await backendApi.toolsets();
        return Array.isArray(toolsets) ? toolsets.filter((item) => typeof item === 'string' && item) : [];
      } catch {
        return [];
      }
    },
    showStatus: setStatus,
  };

  panes.sidebar = createNearbySidebar({ store, actions });
  panes.chat = createNearbyChat({ store, actions, workspace });

  function renderAll(): void {
    panes.sidebar?.render();
    panes.chat?.render();
  }

  const disposeEvent = bridge?.onNearbyEvent?.((event: { type: string; [key: string]: unknown }) => {
    const note = store.applyEvent(event);
    if (note) {
      setStatus(note.text, note.tone);
      if (note.tone === 'error' && /蓝牙|bluetooth|BLE|Nearby 服务已退出|adapter/i.test(note.text)) {
        showBluetoothBanner(note.text);
      }
    }
    if (event.type === 'ready') {
      setStatus(store.discoverable ? '正在查找附近 Ace' : '仅查找附近 Ace');
    }
    if (event.type === 'peer_connected') panes.chat?.focusComposer();
    renderAll();
  }) ?? (() => undefined);

  const sidebar = panes.sidebar;
  const chat = panes.chat;
  workspace.append(sidebar.element, chat.element, chat.panelElement);
  const unsubscribeStore = store.subscribe(renderAll);
  renderAll();

  let active = false;
  return {
    activate(): void {
      active = true;
      void bridge?.nearbyStart?.()
        .catch((error: unknown) => {
          setStatus(`同伴服务启动失败：${errorMessage(error)}`, 'error');
          showBluetoothBanner(errorMessage(error));
        });
    },
    dispose(): void {
      const wasActive = active;
      active = false;
      disposeEvent();
      unsubscribeStore();
      sidebar.dispose();
      chat.dispose();
      if (wasActive) void bridge?.nearbyStop?.();
      root.replaceChildren();
    },
  };
}
