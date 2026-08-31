/**
 * 同伴页接线层：Link 事件写入状态，管理中枢发出的动作交给 Nearby 与主对话。
 */

import { backendApi, type CompanionConversationBinding } from '../backend-client';
import {
  conversationAdapters,
  type ConversationComposerContext,
  type ConversationMentionTarget,
} from './conversation-adapters';
import { openSessionInChat, renderChat } from './chat-controller';
import { loadBackendHistory } from './session-controller';
import { sessionStore } from '../stores/stores';
import { createCompanionHub, type CompanionHubActions } from './companion-hub';
import { AGENT_MODE_LABELS, roomConversationId, NearbyStore, type NearbyAgentMode } from './nearby-store';

const AGENT_TARGET_CAPABILITY = 'agent.target.v1';

export interface NearbyPage {
  activate(): void;
  dispose(): void;
}

type NearbyCommandPayload = Parameters<NonNullable<Window['Crew']['nearbyCommand']>>[0];

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function newRoomId(): string {
  return `room_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function bluetoothHelpText(): string {
  switch (window.Crew?.runtimePlatform) {
    case 'darwin':
      return '请在「系统设置 → 隐私与安全性 → 蓝牙」中允许 Ace 使用蓝牙，并确认蓝牙已开启。';
    case 'win32':
      return '请在「设置 → 蓝牙和设备」中开启蓝牙，并允许 Ace 访问蓝牙。';
    case 'linux':
      return '请确认蓝牙服务已启动且适配器可用。';
    default:
      return '请确认系统蓝牙已开启，并允许 Ace 使用蓝牙。';
  }
}

export function mountNearbyPage(root: HTMLElement, bridge: Window['Crew'] = window.Crew): NearbyPage {
  const store = new NearbyStore();
  const companionBindings = new Map<string, CompanionConversationBinding>();
  const projectedFileIds = new Set<string>();
  const page = document.createElement('div');
  page.className = 'nearby-page companion-page';

  const banner = document.createElement('div');
  banner.className = 'companion-error-banner';
  banner.hidden = true;
  const bannerText = document.createElement('span');
  const bannerDismiss = document.createElement('button');
  bannerDismiss.type = 'button';
  bannerDismiss.textContent = '知道了';
  bannerDismiss.addEventListener('click', () => { banner.hidden = true; });
  banner.append(bannerText, bannerDismiss);

  const liveStatus = document.createElement('div');
  liveStatus.className = 'companion-live-status';
  liveStatus.setAttribute('role', 'status');
  liveStatus.setAttribute('aria-live', 'polite');
  liveStatus.hidden = true;
  let statusTimer: number | null = null;

  function setStatus(text: string, tone: 'normal' | 'error' = 'normal'): void {
    if (statusTimer !== null) window.clearTimeout(statusTimer);
    liveStatus.textContent = text;
    liveStatus.dataset.tone = tone;
    liveStatus.hidden = false;
    statusTimer = window.setTimeout(() => { liveStatus.hidden = true; }, tone === 'error' ? 7000 : 3200);
  }

  function showBluetoothBanner(message: string): void {
    bannerText.textContent = `蓝牙不可用或权限未授予（${message}）。${bluetoothHelpText()}`;
    banner.hidden = false;
  }

  function rememberBinding(binding: CompanionConversationBinding, preview = ''): void {
    companionBindings.set(binding.session_id, binding);
    const current = sessionStore.get().sessions;
    const existing = current.find((item) => item.id === binding.session_id);
    const session = {
      id: binding.session_id,
      title: binding.title,
      updatedAt: Date.now(),
      preview: preview || existing?.preview || '',
      badge: '同伴',
      workspaceId: binding.workspace_id,
      agentLabel: { name: binding.title, provider: 'companion' },
    };
    sessionStore.set({
      sessions: existing
        ? current.map((item) => item.id === binding.session_id ? { ...item, ...session } : item)
        : [session, ...current],
    });
  }

  function syncIncomingProjection(
    result: { appended?: boolean; binding?: CompanionConversationBinding },
    preview: string,
  ): void {
    if (!result.binding) return;
    rememberBinding(result.binding, preview);
    if (result.appended && sessionStore.get().activeSessionId === result.binding.session_id) {
      void loadBackendHistory(result.binding.session_id).then(() => {
        if (sessionStore.get().activeSessionId === result.binding?.session_id) renderChat();
      });
    }
  }

  function command(payload: NearbyCommandPayload): void {
    void Promise.resolve(bridge?.nearbyCommand?.(payload))
      .catch((error: unknown) => setStatus(`操作失败：${errorMessage(error)}`, 'error'));
  }

  async function chooseWorkspace(): Promise<string | null> {
    const workspaces = (await backendApi.workspaces()).filter((item) => !item.hidden);
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'companion-sheet-overlay';
      const sheet = document.createElement('section');
      sheet.className = 'companion-sheet companion-workspace-sheet';
      sheet.setAttribute('role', 'dialog');
      sheet.setAttribute('aria-modal', 'true');
      const title = document.createElement('h2');
      title.textContent = '选择 Agent 工作空间';
      const note = document.createElement('p');
      note.textContent = 'Agent 执行命令和读写文件时使用这个工作空间。未指定时使用“同伴空间”。';
      const list = document.createElement('div');
      list.className = 'companion-workspace-list';
      let selected = 'companion';
      const choices: typeof workspaces = workspaces.some((item) => item.id === 'companion')
        ? workspaces
        : [{ id: 'companion', name: '同伴空间', description: '同伴会话的默认隔离空间', instructions: '' }, ...workspaces];
      for (const workspace of choices.sort((left, right) => (
        left.id === 'companion' ? -1 : right.id === 'companion' ? 1 : left.name.localeCompare(right.name)
      ))) {
        const label = document.createElement('label');
        label.className = 'companion-workspace-option';
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = 'companion-workspace';
        radio.value = workspace.id;
        radio.checked = workspace.id === selected;
        radio.addEventListener('change', () => { selected = radio.value; });
        const copy = document.createElement('span');
        const name = document.createElement('strong');
        name.textContent = workspace.name;
        const detail = document.createElement('small');
        detail.textContent = workspace.id === 'companion'
          ? '默认 · 为同伴会话隔离执行环境'
          : (workspace.root_path || workspace.description || '项目工作空间');
        copy.append(name, detail);
        label.append(radio, copy);
        list.append(label);
      }
      const footer = document.createElement('footer');
      footer.className = 'companion-sheet__footer';
      const finish = (value: string | null): void => {
        overlay.remove();
        resolve(value);
      };
      const cancel = document.createElement('button');
      cancel.type = 'button';
      cancel.className = 'companion-button';
      cancel.textContent = '取消';
      cancel.addEventListener('click', () => finish(null));
      const confirm = document.createElement('button');
      confirm.type = 'button';
      confirm.className = 'companion-button is-primary';
      confirm.textContent = '进入主对话';
      confirm.addEventListener('click', () => finish(selected));
      footer.append(cancel, confirm);
      sheet.append(title, note, list, footer);
      overlay.append(sheet);
      overlay.addEventListener('click', (event) => {
        if (event.target === overlay) finish(null);
      });
      page.append(overlay);
      confirm.focus();
    });
  }

  async function openConversation(conversationId: string): Promise<void> {
    const conversation = store.conversations.get(conversationId);
    if (!conversation) return;
    if (!store.isConversationOnline(conversation)) {
      setStatus(
        conversation.kind === 'dm'
          ? '同伴暂时离线，重新连接后才能发消息'
          : '群内暂无其他在线同伴，暂时不能发送消息',
        'error',
      );
      return;
    }
    const requestedWorkspace = await chooseWorkspace();
    if (requestedWorkspace === null) return;
    const sessionTitle = conversation.kind === 'dm'
      ? `${conversation.title} · 同伴本人`
      : conversation.title;
    const binding = await backendApi.companionOpenConversation({
      kind: conversation.kind === 'dm' ? 'nearby_dm' : 'nearby_room',
      target_id: conversation.kind === 'dm' ? conversation.peerId : conversation.roomId,
      title: sessionTitle,
      workspace_id: requestedWorkspace,
    });
    rememberBinding({ ...binding, title: binding.title || sessionTitle });
    await openSessionInChat(binding.session_id);
  }

  const actions: CompanionHubActions = {
    connectPeer(peerId) {
      const peer = store.peers.get(peerId);
      if (!peer || peer.connection === 'connected' || peer.connection === 'connecting') return;
      store.peers.set(peerId, { ...peer, connection: 'connecting' });
      setStatus(`正在连接 ${store.peerLabel(peerId)}…`);
      command({ type: 'connect_peer', peer_id: peerId });
      hub.render();
    },
    disconnectPeer(peerId) {
      command({ type: 'disconnect_peer', peer_id: peerId });
    },
    setDiscoverable(enabled) {
      command({ type: 'set_discoverable', enabled });
    },
    openConversation(conversationId) {
      void openConversation(conversationId)
        .catch((error: unknown) => setStatus(`打开同伴会话失败：${errorMessage(error)}`, 'error'));
    },
    createRoom(name: string, memberIds: string[], agentMode: NearbyAgentMode) {
      command({
        type: 'create_room',
        room_id: newRoomId(),
        room_name: name,
        peer_ids: memberIds,
        agent_mode: agentMode,
      });
    },
    async loadAgentCandidates() {
      return (await backendApi.companionProfile()).agent_candidates ?? [];
    },
    async savePublishedAgents(sourceRefs) {
      const result = await backendApi.companionUpdatePublications(sourceRefs);
      await bridge.nearbyStop?.();
      await bridge.nearbyStart?.();
      return result.agent_candidates ?? [];
    },
    showStatus: setStatus,
  };

  const hub = createCompanionHub({ store, actions });
  page.append(banner, hub.element, liveStatus);
  root.replaceChildren(page);

  function companionComposerContext(sessionId: string): ConversationComposerContext | null {
    const binding = companionBindings.get(sessionId);
    if (!binding || binding.kind !== 'nearby_room') return null;
    const conversation = store.conversations.get(roomConversationId(binding.target_id));
    if (!conversation) return null;
    const memberIds = conversation.memberIds.includes(store.localPeerId)
      ? conversation.memberIds
      : [store.localPeerId, ...conversation.memberIds];
    return {
      title: conversation.title,
      modeLabel: AGENT_MODE_LABELS[conversation.agentMode],
      members: memberIds.map((peerId) => {
        const isSelf = peerId === store.localPeerId;
        const peer = isSelf ? null : store.peers.get(peerId);
        const connection = isSelf ? 'connected' : peer?.connection ?? 'disconnected';
        const state = connection === 'connected'
          ? 'online'
          : connection === 'connecting'
            ? 'connecting'
            : 'offline';
        const agents = isSelf ? store.localPublishedAgents : peer?.published_agents ?? [];
        const supportsSpecificTarget = isSelf || peer?.capabilities.includes(AGENT_TARGET_CAPABILITY) === true;
        return {
          peerId,
          label: isSelf ? store.localName : store.peerLabel(peerId),
          isSelf,
          state,
          stateLabel: isSelf ? '本机在线' : state === 'online' ? '在线' : state === 'connecting' ? '连接中' : '离线',
          ...(isSelf ? {} : peer?.avatar ? { avatar: peer.avatar } : {}),
          agents: agents.map((agent, index) => {
            const pending = store.isAgentReplyPending(conversation.id, peerId, agent.public_agent_id);
            const canRoute = supportsSpecificTarget || index === 0;
            const unavailable = state !== 'online' || !canRoute;
            return {
              kind: 'agent' as const,
              peerId,
              publicAgentId: agent.public_agent_id,
              label: agent.display_name,
              ownerLabel: isSelf ? store.localName : store.peerLabel(peerId),
              routing: supportsSpecificTarget ? 'specific' as const : 'peer-default' as const,
              state: isSelf ? 'local' as const : pending ? 'working' as const : unavailable ? 'unavailable' as const : 'available' as const,
              stateLabel: isSelf ? '本机' : pending ? '处理中' : state !== 'online' ? '主人离线' : canRoute ? '可用' : '需对方升级',
              ...(unavailable
                ? { disabledReason: state !== 'online' ? 'Agent 的主人当前离线' : '对方版本不支持指定多个 Agent' }
                : {}),
              ...(agent.avatar ? { avatar: agent.avatar } : {}),
            };
          }),
        };
      }),
    };
  }

  const unregisterConversationAdapter = conversationAdapters.register({
    id: 'companion',
    matches: (sessionId) => sessionId.startsWith('agent:main:nearby:'),
    abilities: (sessionId) => {
      const binding = companionBindings.get(sessionId);
      const capabilities = companionBindings.get(sessionId)?.capabilities;
      const conversation = binding
        ? store.conversations.get(
          binding.kind === 'nearby_room' ? roomConversationId(binding.target_id) : `dm:${binding.target_id}`,
        )
        : null;
      const online = Boolean(conversation && store.isConversationOnline(conversation));
      const fileRecipients = conversation
        ? (conversation.kind === 'dm'
          ? [conversation.peerId]
          : conversation.memberIds.filter((peerId) => (
            peerId !== store.localPeerId && store.peers.get(peerId)?.connection === 'connected'
          )))
        : [];
      const supportsFastFile = fileRecipients.length > 0 && fileRecipients.every((peerId) => (
        store.peers.get(peerId)?.capabilities.includes('file.webrtc') === true
      ));
      return {
        canSendText: online && (capabilities?.can_send_text ?? true),
        canAttach: online && supportsFastFile && (capabilities?.can_attach ?? true),
        canMentionPeople: capabilities?.can_mention_people ?? sessionId.includes(':room:'),
        canMentionAgents: capabilities?.can_mention_agents ?? sessionId.includes(':room:'),
        showModelPicker: capabilities?.show_model_picker ?? false,
        showSkills: capabilities?.show_skills ?? false,
        showPlanMode: capabilities?.show_plan_mode ?? false,
        ...(!online ? {
          unavailableReason: conversation?.kind === 'room'
            ? '群内暂无其他在线同伴，暂时不能发送消息'
            : '同伴暂时离线，重新连接后才能发消息',
        } : {}),
      };
    },
    composerContext: companionComposerContext,
    subscribe: (listener) => store.subscribe(listener),
    async send({ sessionId, text, attachments, mentions: selectedMentions = [] }) {
      const binding = companionBindings.get(sessionId);
      if (!binding) throw new Error('同伴会话尚未绑定，请从同伴页重新打开');
      const conversation = store.conversations.get(
        binding.kind === 'nearby_room' ? roomConversationId(binding.target_id) : `dm:${binding.target_id}`,
      );
      if (!conversation || !store.isConversationOnline(conversation)) {
        throw new Error(
          binding.kind === 'nearby_room'
            ? '群内暂无其他在线同伴，暂时不能发送消息'
            : '同伴暂时离线，重新连接后才能发消息',
        );
      }
      const prepared = await Promise.all(attachments.map(async (attachment) => (
        await backendApi.companionPrepareFile(attachment)
      ).file));
      const room = binding.kind === 'nearby_room'
        ? store.conversations.get(roomConversationId(binding.target_id))
        : null;
      const context = room ? companionComposerContext(sessionId) : null;
      const contextAgents = context?.members.flatMap((member) => member.agents) ?? [];
      const inferredAgentMentions: ConversationMentionTarget[] = contextAgents
        .filter((agent) => {
          if (agent.disabledReason) return false;
          const duplicate = contextAgents.some((candidate) => (
            candidate !== agent
            && candidate.label.trim().toLowerCase() === agent.label.trim().toLowerCase()
          ));
          return text.includes(`@${agent.label}${duplicate ? `·${agent.ownerLabel}` : ''}`);
        });
      const agentTargets = [...selectedMentions, ...inferredAgentMentions]
        .filter((target, index, rows) => rows.findIndex((candidate) => (
          candidate.peerId === target.peerId && candidate.publicAgentId === target.publicAgentId
        )) === index);
      const mentions = room
        ? [...new Set([
          ...agentTargets.map((target) => target.peerId),
          ...room.memberIds.filter((peerId) => text.includes(`@${store.peerLabel(peerId)}`)),
        ])]
        : [];
      const agentMentions = agentTargets
        .filter((target) => target.routing === 'specific')
        .map((target) => ({ peer_id: target.peerId, public_agent_id: target.publicAgentId }));
      const receipt = await backendApi.companionSendMessage(
        sessionId,
        text,
        mentions,
        agentMentions,
        attachments,
      );
      try {
        if (text.trim() && binding.kind === 'nearby_dm') {
          await bridge.nearbyCommand?.({
            type: 'send_peer_message', peer_id: binding.target_id, text,
            client_message_id: receipt.event_id, mentions: [],
          });
        } else if (text.trim()) {
          await bridge.nearbyCommand?.({
            type: 'send_room_message', room_id: binding.target_id, text,
            client_message_id: receipt.event_id, mentions,
            ...(agentMentions.length > 0 ? { agent_mentions: agentMentions } : {}),
          });
          if (room?.agentMode === 'mention' && agentTargets.length > 0) {
            store.expectAgentTargets(room.id, agentTargets);
          } else if (room?.agentMode === 'mention' && mentions.length > 0) {
            store.expectAgentReply(room.id, mentions);
          } else if (room?.agentMode === 'auto') {
            store.expectAgentTargets(room.id, agentTargets);
            const targetedPeers = new Set(agentTargets.map((target) => target.peerId));
            store.expectAgentReply(room.id, room.memberIds.filter((peerId) => (
              peerId !== store.localPeerId && !targetedPeers.has(peerId)
            )));
          }
        }
        for (const file of prepared) {
          if (binding.kind === 'nearby_dm') {
            await bridge.nearbyCommand?.({
              type: 'send_peer_file', peer_id: binding.target_id,
              file_id: file.file_id, name: file.name, mime_type: file.mime_type,
              size: file.size, sha256: file.sha256, file_path: file.path,
              client_message_id: receipt.event_id,
            });
          } else {
            await bridge.nearbyCommand?.({
              type: 'send_room_file', room_id: binding.target_id,
              file_id: file.file_id, name: file.name, mime_type: file.mime_type,
              size: file.size, sha256: file.sha256, file_path: file.path,
              client_message_id: receipt.event_id,
              mentions: [],
            });
          }
        }
        await backendApi.companionSettleOutbox(receipt.event_id, 'sent');
      } catch (error) {
        await backendApi.companionSettleOutbox(receipt.event_id, 'failed').catch(() => undefined);
        throw error;
      }
    },
  });
  void backendApi.companionConversations().then((snapshot) => {
    for (const binding of snapshot.conversations ?? []) {
      rememberBinding(binding);
    }
  }).catch(() => undefined);

  function projectLinkEvent(event: { type: string; [key: string]: unknown }): void {
    if (!bridge?.gatewayFetch) return;
    if (event.type === 'message_delivered') {
      const messageId = typeof event.message_id === 'string' ? event.message_id : '';
      if (messageId) {
        void backendApi.companionSettleOutbox(messageId, 'delivered').catch(() => undefined);
      }
      return;
    }
    const peer = event.peer && typeof event.peer === 'object' && !Array.isArray(event.peer)
      ? event.peer as Record<string, unknown>
      : null;
    if (peer && ['peer_discovered', 'peer_connected', 'ready'].includes(event.type)) {
      const peerId = typeof peer.peer_id === 'string' ? peer.peer_id : '';
      if (peerId && peerId !== store.localPeerId) {
        void backendApi.companionLinkState({
          type: 'peer', peer_id: peerId, profile: peer,
          connection_state: event.type === 'peer_connected' ? 'connected' : 'discovered',
        }).catch(() => undefined);
      }
      return;
    }
    if (['peer_disconnected', 'peer_unavailable', 'peer_connection_failed'].includes(event.type)) {
      const peerId = typeof event.peer_id === 'string' ? event.peer_id : '';
      const current = store.peers.get(peerId);
      if (peerId && current) {
        void backendApi.companionLinkState({
          type: 'peer',
          peer_id: peerId,
          profile: {
            peer_id: current.peer_id,
            display_name: current.display_name,
            agent_name: current.agent_name,
            capabilities: current.capabilities,
            published_agents: current.published_agents,
          },
          connection_state: current.connection,
        }).catch(() => undefined);
      }
      return;
    }
    if (['room_created', 'room_joined', 'room_restored', 'room_settings_updated'].includes(event.type)) {
      const roomId = typeof event.room_id === 'string' ? event.room_id : '';
      if (roomId) {
        const conversation = store.conversations.get(roomConversationId(roomId));
        void backendApi.companionLinkState({
          type: 'room', room_id: roomId,
          name: conversation?.title ?? event.room_name ?? '同伴群聊',
          owner_peer_id: conversation?.ownerPeerId ?? event.owner_peer_id ?? '',
          human_member_ids: conversation?.memberIds ?? event.peer_ids ?? [],
        }).catch(() => undefined);
      }
      return;
    }
    let kind: 'nearby_dm' | 'nearby_room' | null = null;
    let targetId = '';
    let text = '';
    let sender = '';
    let senderKind: 'human' | 'agent' = 'human';
    let senderAgentId = '';
    let senderAgentName = '';
    if (event.type === 'peer_message_received') {
      kind = 'nearby_dm';
      targetId = typeof event.peer_id === 'string' ? event.peer_id : '';
      text = typeof event.text === 'string' ? event.text : '';
      sender = targetId;
    } else if (event.type === 'message') {
      const message = event.message && typeof event.message === 'object' && !Array.isArray(event.message)
        ? event.message as Record<string, unknown> : null;
      const payload = message?.payload && typeof message.payload === 'object' && !Array.isArray(message.payload)
        ? message.payload as Record<string, unknown> : null;
      const messageType = typeof message?.type === 'string' ? message.type : '';
      sender = typeof message?.sender === 'string' ? message.sender : '';
      text = typeof payload?.text === 'string' ? payload.text : '';
      if (messageType === 'room.message') {
        kind = 'nearby_room';
        targetId = typeof payload?.room_id === 'string' ? payload.room_id : '';
        const messageId = typeof message?.message_id === 'string' ? message.message_id : '';
        const projected = store.conversationMessages(roomConversationId(targetId))
          .find((item) => item.id === messageId);
        if (projected?.kind === 'agent') {
          senderKind = 'agent';
          senderAgentId = projected.agentPublicId || '';
          senderAgentName = projected.senderName || store.peerAgentLabel(sender);
        }
      } else if (messageType === 'peer.message') {
        kind = 'nearby_dm';
        targetId = typeof event.peer_id === 'string' ? event.peer_id : sender;
      } else if (messageType === 'room.file' || messageType === 'peer.file') {
        kind = messageType === 'room.file' ? 'nearby_room' : 'nearby_dm';
        targetId = messageType === 'room.file'
          ? (typeof payload?.room_id === 'string' ? payload.room_id : '')
          : (typeof event.peer_id === 'string' ? event.peer_id : sender);
        const rawFile = payload?.file && typeof payload.file === 'object' && !Array.isArray(payload.file)
          ? payload.file as Record<string, unknown> : null;
        const fileId = typeof rawFile?.file_id === 'string' ? rawFile.file_id : '';
        const conversationId = kind === 'nearby_room' ? roomConversationId(targetId) : `dm:${targetId}`;
        const file = store.conversationMessages(conversationId)
          .find((item) => item.file?.file_id === fileId && item.file.complete)?.file;
        if (file && !projectedFileIds.has(file.file_id)) {
          projectedFileIds.add(file.file_id);
          const conversation = store.conversations.get(conversationId);
          void backendApi.companionLinkState({
            type: 'file', kind, target_id: targetId,
            message_id: `file:${file.file_id}`,
            sender_kind: 'human', sender_id: sender, sender_name: store.peerLabel(sender),
            conversation_title: conversation?.title ?? '', file,
          }).then((result) => syncIncomingProjection(result, `[文件] ${file.name}`))
            .catch(() => projectedFileIds.delete(file.file_id));
        }
        return;
      }
    }
    if (kind && targetId && text.trim() && sender !== store.localPeerId) {
      const conversation = store.conversations.get(kind === 'nearby_dm' ? `dm:${targetId}` : roomConversationId(targetId));
      const rawMessage = event.message && typeof event.message === 'object' && !Array.isArray(event.message)
        ? event.message as Record<string, unknown> : null;
      const messageId = typeof event.message_id === 'string'
        ? event.message_id
        : (typeof rawMessage?.message_id === 'string' ? rawMessage.message_id : '');
      void backendApi.companionLinkState({
        type: 'message', kind, target_id: targetId, text, message_id: messageId,
        sender_kind: senderKind, sender_id: senderKind === 'agent' ? senderAgentId || sender : sender,
        sender_name: senderKind === 'agent' ? senderAgentName : store.peerLabel(sender),
        conversation_title: conversation?.title ?? '',
      }).then((result) => syncIncomingProjection(result, text)).catch(() => undefined);
    }
  }

  const disposeEvent = bridge?.onNearbyEvent?.((event: { type: string; [key: string]: unknown }) => {
    const note = store.applyEvent(event);
    projectLinkEvent(event);
    if (note) {
      setStatus(note.text, note.tone);
      if (note.tone === 'error' && /蓝牙|bluetooth|BLE|Nearby 服务已退出|adapter/i.test(note.text)) {
        showBluetoothBanner(note.text);
      }
    }
    if (event.type === 'peer_connected') {
      const peer = event.peer && typeof event.peer === 'object' && !Array.isArray(event.peer)
        ? event.peer as Record<string, unknown> : null;
      const peerId = typeof peer?.peer_id === 'string' ? peer.peer_id : '';
      if (peerId) hub.showConversation(`dm:${peerId}`);
    } else if (event.type === 'room_created' || event.type === 'room_joined') {
      const roomId = typeof event.room_id === 'string' ? event.room_id : '';
      if (roomId) hub.showConversation(roomConversationId(roomId));
    } else {
      hub.render();
    }
  }) ?? (() => undefined);
  const unsubscribeStore = store.subscribe(() => hub.render());
  hub.render();

  let active = false;
  return {
    activate(): void {
      active = true;
      void bridge?.nearbyStart?.().catch((error: unknown) => {
        setStatus(`同伴服务启动失败：${errorMessage(error)}`, 'error');
        showBluetoothBanner(errorMessage(error));
      });
    },
    dispose(): void {
      const wasActive = active;
      active = false;
      if (statusTimer !== null) window.clearTimeout(statusTimer);
      disposeEvent();
      unsubscribeStore();
      hub.dispose();
      unregisterConversationAdapter();
      if (wasActive) void bridge?.nearbyStop?.();
      root.replaceChildren();
    },
  };
}
