interface NearbyPeer {
  peer_id: string;
  display_name: string;
  agent_name: string;
  capabilities: string[];
  connection: 'discovered' | 'connected' | 'disconnected';
}

interface NearbyRoom {
  roomId: string;
  roomName: string;
  peerIds: string[];
}

interface NearbyReplyReference {
  message_id: string;
  sender: string;
  text: string;
}

interface NearbyFileChunk {
  file_id: string;
  name: string;
  mime_type: string;
  size: number;
  sha256: string;
  chunk_index: number;
  chunk_total: number;
  data_base64: string;
}

interface NearbyMessagePayload {
  room_id?: string;
  text?: string;
  mentions?: string[];
  reply_to?: NearbyReplyReference;
  file?: NearbyFileChunk;
}

interface NearbyMessage {
  message_id: string;
  sender: string;
  message_type: string;
  payload: NearbyMessagePayload;
}

interface NearbyEvent {
  type: string;
  peer?: NearbyPeer;
  peer_id?: string;
  room_id?: string;
  room_name?: string;
  peer_ids?: string[];
  discoverable?: boolean;
  message?: NearbyMessage | string;
}

interface NearbyFileSelection {
  file_id: string;
  name: string;
  mime_type: string;
  size: number;
  sha256: string;
  data_base64: string;
}

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

function makeRoomId(): string {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID().replaceAll('-', '').slice(0, 16)
    : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
  return `room_${random}`;
}

function formatFileSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function memberLabel(peer: NearbyPeer | undefined, peerId: string): string {
  return peer?.display_name || peer?.agent_name || peerId;
}

function mentionsInText(text: string, members: NearbyPeer[], localPeerId: string): string[] {
  const candidates = members
    .filter((peer) => peer.peer_id !== localPeerId)
    .flatMap((peer) => [
      { name: peer.display_name, peerId: peer.peer_id },
      { name: peer.agent_name, peerId: peer.peer_id },
      { name: peer.peer_id, peerId: peer.peer_id },
    ])
    .filter((candidate) => candidate.name.trim())
    .sort((a, b) => b.name.length - a.name.length);
  const found = new Set<string>();
  for (const match of text.matchAll(/(^|\s)@/g)) {
    const matchStart = match.index ?? 0;
    const start = matchStart + match[0].length;
    const rest = text.slice(start);
    const known = candidates.find((candidate) => {
      if (rest.slice(0, candidate.name.length).toLocaleLowerCase() !== candidate.name.toLocaleLowerCase()) return false;
      const next = rest[candidate.name.length] ?? '';
      return !next || /[\s,;.!?:)\]}]/.test(next);
    });
    if (known) {
      found.add(known.peerId);
      continue;
    }
    const token = rest.match(/^[A-Za-z0-9_.:-]+/)?.[0]?.toLocaleLowerCase();
    if (!token) continue;
    const fallback = candidates.find((candidate) => candidate.name.toLocaleLowerCase() === token);
    if (fallback) found.add(fallback.peerId);
  }
  return [...found];
}

export interface NearbyPage {
  activate(): void;
  dispose(): void;
}

export function mountNearbyPage(root: HTMLElement, bridge: Window['Crew'] = window.Crew): NearbyPage {
  const page = document.createElement('div');
  page.className = 'nearby-page';
  const header = document.createElement('header');
  header.className = 'nearby-page__header';
  const title = textElement('h1', 'nearby-page__title', '同伴');
  const subtitle = textElement('p', 'nearby-page__subtitle', '发现附近的 Agent，选择后进入群聊。');
  const status = textElement('span', 'nearby-page__status', '准备发现');
  header.append(title, subtitle, status);

  const privacy = document.createElement('section');
  privacy.className = 'nearby-privacy';
  const discoverabilityToggle = document.createElement('input');
  discoverabilityToggle.type = 'checkbox';
  discoverabilityToggle.className = 'nearby-privacy__toggle';
  discoverabilityToggle.checked = true;
  discoverabilityToggle.disabled = true;
  const privacyCopy = document.createElement('span');
  privacyCopy.className = 'nearby-privacy__copy';
  privacyCopy.append(
    textElement('strong', 'nearby-privacy__title', '允许附近设备发现我的 Agent'),
    textElement('span', 'nearby-privacy__hint', '关闭后停止 BLE 广播，但不会断开已建立的群聊。只公开名称、能力和协议版本，不公开工作目录、环境变量或密钥。'),
  );
  const privacyLabel = document.createElement('label');
  privacyLabel.className = 'nearby-privacy__label';
  privacyLabel.append(discoverabilityToggle, privacyCopy);
  privacy.append(privacyLabel);

  const discovery = document.createElement('section');
  discovery.className = 'nearby-discovery';
  const discoveryHeader = document.createElement('div');
  discoveryHeader.className = 'nearby-section__header';
  discoveryHeader.append(
    textElement('h2', 'nearby-section__title', '附近的 Agent'),
    textElement('span', 'nearby-section__hint', 'BLE · 跨平台'),
  );
  const peerList = document.createElement('div');
  peerList.className = 'nearby-peer-list';
  peerList.append(textElement('p', 'nearby-empty', '正在寻找附近的 Agent…'));
  const roomName = document.createElement('input');
  roomName.className = 'nearby-room-name';
  roomName.type = 'text';
  roomName.value = '同伴群聊';
  roomName.maxLength = 120;
  roomName.setAttribute('aria-label', '群聊名称');
  const createRoom = document.createElement('button');
  createRoom.type = 'button';
  createRoom.className = 'nearby-primary-action';
  createRoom.textContent = '选择 Agent 开始群聊';
  createRoom.disabled = true;
  const discoveryActions = document.createElement('div');
  discoveryActions.className = 'nearby-discovery__actions';
  discoveryActions.append(roomName, createRoom);
  discovery.append(discoveryHeader, peerList, discoveryActions);

  const room = document.createElement('section');
  room.className = 'nearby-room';
  room.hidden = true;
  const roomHeader = document.createElement('header');
  roomHeader.className = 'nearby-room__header';
  const roomTitle = textElement('h2', 'nearby-room__title', '同伴群聊');
  const leaveRoom = document.createElement('button');
  leaveRoom.type = 'button';
  leaveRoom.className = 'nearby-secondary-action';
  leaveRoom.textContent = '退出群聊';
  roomHeader.append(roomTitle, leaveRoom);
  const roomMembers = textElement('p', 'nearby-room__members', '');
  const roomMessages = document.createElement('div');
  roomMessages.className = 'nearby-room__messages';

  const replyBar = document.createElement('div');
  replyBar.className = 'nearby-room__reply';
  replyBar.hidden = true;
  const replyLabel = textElement('span', 'nearby-room__reply-label', '');
  const cancelReply = document.createElement('button');
  cancelReply.type = 'button';
  cancelReply.className = 'nearby-room__reply-cancel';
  cancelReply.textContent = '取消';
  replyBar.append(replyLabel, cancelReply);

  const roomForm = document.createElement('form');
  roomForm.className = 'nearby-room__form';
  const messageInput = document.createElement('input');
  messageInput.type = 'text';
  messageInput.className = 'nearby-room__input';
  messageInput.placeholder = '输入消息…';
  messageInput.autocomplete = 'off';
  const mentionButton = document.createElement('button');
  mentionButton.type = 'button';
  mentionButton.className = 'nearby-composer-action';
  mentionButton.textContent = '@';
  mentionButton.title = '提及成员';
  const fileButton = document.createElement('button');
  fileButton.type = 'button';
  fileButton.className = 'nearby-composer-action';
  fileButton.textContent = '文件';
  fileButton.title = '发送文件';
  const sendMessage = document.createElement('button');
  sendMessage.type = 'submit';
  sendMessage.className = 'nearby-primary-action';
  sendMessage.textContent = '发送';
  const mentionMenu = document.createElement('div');
  mentionMenu.className = 'nearby-mention-menu';
  mentionMenu.hidden = true;
  roomForm.append(mentionButton, fileButton, messageInput, sendMessage, mentionMenu);
  room.append(roomHeader, roomMembers, roomMessages, replyBar, roomForm);
  page.append(header, privacy, discovery, room);
  root.replaceChildren(page);

  const peers = new Map<string, NearbyPeer>();
  const selected = new Set<string>();
  const selectedMentions = new Set<string>();
  const messages: NearbyMessage[] = [];
  const seenRenderedMessages = new Set<string>();
  const fileTransfers = new Map<string, {
    message: NearbyMessage;
    chunks: Array<string | undefined>;
  }>();
  let currentRoom: NearbyRoom | null = null;
  let replyTarget: NearbyReplyReference | null = null;
  let localPeerId = '';
  let active = false;
  let discoverabilityKnown = false;
  let pendingDiscoverability: boolean | null = null;

  const setStatus = (value: string, tone: 'normal' | 'error' = 'normal'): void => {
    status.textContent = value;
    status.dataset.tone = tone;
  };

  const selectedConnectedPeers = (): NearbyPeer[] => Array.from(selected)
    .map((peerId) => peers.get(peerId))
    .filter((peer): peer is NearbyPeer => Boolean(peer && peer.connection === 'connected'));

  const roomPeerList = (): NearbyPeer[] => currentRoom?.peerIds
    .map((peerId) => peers.get(peerId))
    .filter((peer): peer is NearbyPeer => Boolean(peer)) ?? [];

  const renderPeers = (): void => {
    peerList.replaceChildren();
    if (peers.size === 0) peerList.append(textElement('p', 'nearby-empty', '正在寻找附近的 Agent…'));
    for (const peer of peers.values()) {
      const label = document.createElement('label');
      label.className = 'nearby-peer-card';
      label.dataset.connection = peer.connection;
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = selected.has(peer.peer_id);
      checkbox.disabled = peer.connection !== 'connected';
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) selected.add(peer.peer_id);
        else selected.delete(peer.peer_id);
        renderPeers();
      });
      const copy = document.createElement('span');
      copy.className = 'nearby-peer-card__copy';
      copy.append(
        textElement('strong', 'nearby-peer-card__name', peer.display_name || peer.peer_id),
        textElement('span', 'nearby-peer-card__agent', peer.agent_name || 'Crew Agent'),
        textElement('span', 'nearby-peer-card__capabilities', peer.capabilities?.join(' · ') || '可进行群聊'),
        textElement('span', 'nearby-peer-card__workspace', '工作目录：对方本机私有'),
      );
      const connection = textElement('span', 'nearby-peer-card__connection', peer.connection === 'connected' ? '可选择' : '连接中');
      label.append(checkbox, copy, connection);
      peerList.append(label);
    }
    createRoom.disabled = selectedConnectedPeers().length === 0;
  };

  const renderRoom = (): void => {
    const activeRoom = currentRoom;
    room.hidden = !activeRoom;
    discovery.hidden = Boolean(activeRoom);
    if (!activeRoom) return;
    roomTitle.textContent = activeRoom.roomName;
    const names = activeRoom.peerIds.map((peerId) => peerId === localPeerId ? '我' : memberLabel(peers.get(peerId), peerId));
    roomMembers.textContent = `${activeRoom.peerIds.length} 位成员 · ${names.join('、')}`;
    renderMentionMenu();
  };

  const setReplyTarget = (message: NearbyMessage | null): void => {
    if (!message) {
      replyTarget = null;
      replyBar.hidden = true;
      replyLabel.textContent = '';
      return;
    }
    const text = message.payload.text || message.payload.file?.name || '';
    replyTarget = {
      message_id: message.message_id,
      sender: message.sender,
      text: text.slice(0, 500),
    };
    replyLabel.textContent = `回复 ${memberLabel(peers.get(message.sender), message.sender)}：${text.slice(0, 100)}`;
    replyBar.hidden = false;
    messageInput.focus();
  };

  const renderMessage = (message: NearbyMessage, peerId: string): HTMLElement => {
    const item = document.createElement('article');
    const own = peerId === localPeerId || message.sender === localPeerId;
    item.className = `nearby-message${own ? ' nearby-message--own' : ''}`;
    item.dataset.messageId = message.message_id;
    const sender = textElement('span', 'nearby-message__sender', own ? '我' : memberLabel(peers.get(peerId), peerId));
    item.append(sender);
    const reply = message.payload.reply_to;
    if (reply) {
      const quote = textElement('div', 'nearby-message__reply', `回复 ${memberLabel(peers.get(reply.sender), reply.sender)}：${reply.text}`);
      item.append(quote);
    }
    if (message.payload.text) {
      item.append(textElement('p', 'nearby-message__text', message.payload.text));
    }
    const file = message.payload.file;
    if (file) {
      const fileCard = document.createElement('div');
      fileCard.className = 'nearby-message__file';
      fileCard.append(
        textElement('strong', 'nearby-message__file-name', file.name),
        textElement('span', 'nearby-message__file-size', formatFileSize(file.size)),
      );
      const save = document.createElement('button');
      save.type = 'button';
      save.className = 'nearby-message__file-save';
      save.textContent = '保存文件';
      save.addEventListener('click', () => {
        void bridge?.nearbySaveFile?.({
          name: file.name,
          mime_type: file.mime_type,
          size: file.size,
          sha256: file.sha256,
          data_base64: file.data_base64,
        })
          .then((result) => {
            if (result?.ok) setStatus(`已保存：${file.name}`);
          })
          .catch((error: unknown) => setStatus(`保存失败：${error instanceof Error ? error.message : String(error)}`, 'error'));
      });
      fileCard.append(save);
      item.append(fileCard);
    }
    const mentionIds = message.payload.mentions ?? [];
    if (mentionIds.length) {
      const labels = mentionIds.map((id) => `@${memberLabel(peers.get(id), id)}`);
      item.append(textElement('span', 'nearby-message__mentions', `提及 ${labels.join('、')}`));
    }
    const replyButton = document.createElement('button');
    replyButton.type = 'button';
    replyButton.className = 'nearby-message__reply-action';
    replyButton.textContent = '回复';
    replyButton.addEventListener('click', () => setReplyTarget(message));
    item.append(replyButton);
    return item;
  };

  const renderMessages = (): void => {
    roomMessages.replaceChildren();
    for (const message of messages) {
      roomMessages.append(renderMessage(message, message.sender));
    }
    roomMessages.scrollTop = roomMessages.scrollHeight;
  };

  const appendMessage = (message: NearbyMessage): void => {
    if (seenRenderedMessages.has(message.message_id)) return;
    seenRenderedMessages.add(message.message_id);
    messages.push(message);
    renderMessages();
  };

  const receiveFileChunk = (message: NearbyMessage): void => {
    const file = message.payload.file;
    if (!file || file.chunk_total <= 0 || file.chunk_index < 0 || file.chunk_index >= file.chunk_total) return;
    let transfer = fileTransfers.get(file.file_id);
    if (!transfer) {
      transfer = { message, chunks: Array.from({ length: file.chunk_total }) };
      fileTransfers.set(file.file_id, transfer);
    }
    transfer.chunks[file.chunk_index] = file.data_base64;
    if (transfer.chunks.some((chunk) => chunk === undefined)) {
      setStatus(`正在接收文件：${file.name}`);
      return;
    }
    const complete: NearbyMessage = {
      ...transfer.message,
      message_id: `file:${file.file_id}`,
      payload: {
        ...transfer.message.payload,
        file: { ...file, data_base64: transfer.chunks.join('') },
      },
    };
    fileTransfers.delete(file.file_id);
    appendMessage(complete);
    setStatus(`收到文件：${file.name}`);
  };

  const renderMentionMenu = (): void => {
    mentionMenu.replaceChildren();
    for (const peer of roomPeerList().filter((candidate) => candidate.peer_id !== localPeerId)) {
      const option = document.createElement('button');
      option.type = 'button';
      option.className = 'nearby-mention-menu__item';
      option.textContent = `@${memberLabel(peer, peer.peer_id)}`;
      option.addEventListener('click', () => {
        const start = messageInput.selectionStart ?? messageInput.value.length;
        const end = messageInput.selectionEnd ?? start;
        messageInput.value = `${messageInput.value.slice(0, start)}@${memberLabel(peer, peer.peer_id)} ${messageInput.value.slice(end)}`;
        messageInput.selectionStart = messageInput.selectionEnd = start + memberLabel(peer, peer.peer_id).length + 2;
        selectedMentions.add(peer.peer_id);
        mentionMenu.hidden = true;
        messageInput.focus();
      });
      mentionMenu.append(option);
    }
    if (!mentionMenu.childElementCount) mentionMenu.append(textElement('span', 'nearby-mention-menu__empty', '当前房间没有可提及的成员'));
  };

  const onEvent = (event: { type: string; [key: string]: unknown }): void => {
    const nearbyEvent = event as NearbyEvent;
    switch (nearbyEvent.type) {
      case 'ready': {
        const peer = nearbyEvent.peer as NearbyPeer | undefined;
        if (peer) localPeerId = peer.peer_id;
        discoverabilityToggle.checked = nearbyEvent.discoverable !== false;
        discoverabilityToggle.disabled = false;
        discoverabilityKnown = true;
        setStatus(nearbyEvent.discoverable === false ? '仅扫描附近 Agent' : '正在发现附近 Agent');
        break;
      }
      case 'discovery_started':
        setStatus('正在发现附近 Agent');
        break;
      case 'discovery_stopped':
        setStatus('发现已暂停');
        break;
      case 'discoverability_changed':
        discoverabilityToggle.checked = nearbyEvent.discoverable !== false;
        discoverabilityToggle.disabled = false;
        discoverabilityKnown = true;
        pendingDiscoverability = null;
        setStatus(nearbyEvent.discoverable === false ? '已停止被附近设备发现' : '已允许附近设备发现');
        break;
      case 'peer_discovered':
      case 'peer_connected': {
        const peer = nearbyEvent.peer as NearbyPeer | undefined;
        if (!peer) break;
        peers.set(peer.peer_id, { ...peer, connection: nearbyEvent.type === 'peer_connected' ? 'connected' : (peers.get(peer.peer_id)?.connection ?? 'discovered') });
        if (nearbyEvent.type === 'peer_connected') setStatus('已发现可加入群聊的 Agent');
        renderPeers();
        renderMentionMenu();
        break;
      }
      case 'peer_disconnected': {
        const peerId = String(nearbyEvent.peer_id || '');
        const peer = peers.get(peerId);
        if (peer) peers.set(peerId, { ...peer, connection: 'disconnected' });
        selected.delete(peerId);
        renderPeers();
        renderMentionMenu();
        break;
      }
      case 'room_created':
      case 'room_joined': {
        currentRoom = {
          roomId: String(nearbyEvent.room_id || ''),
          roomName: String(nearbyEvent.room_name || '同伴群聊'),
          peerIds: Array.isArray(nearbyEvent.peer_ids) ? nearbyEvent.peer_ids.map(String) : [],
        };
        renderRoom();
        setStatus('群聊已连接');
        break;
      }
      case 'room_left':
        currentRoom = null;
        messages.splice(0, messages.length);
        seenRenderedMessages.clear();
        fileTransfers.clear();
        setReplyTarget(null);
        renderRoom();
        roomMessages.replaceChildren();
        setStatus('已退出群聊');
        break;
      case 'message': {
        const message = typeof nearbyEvent.message === 'object'
          ? nearbyEvent.message as NearbyMessage
          : undefined;
        if (!message || !currentRoom || message.payload?.room_id !== currentRoom.roomId) break;
        if (message.message_type === 'room.file') receiveFileChunk(message);
        else if (message.message_type === 'room.message') appendMessage(message);
        break;
      }
      case 'error':
        if (pendingDiscoverability !== null) {
          discoverabilityToggle.checked = !pendingDiscoverability;
          pendingDiscoverability = null;
          discoverabilityToggle.disabled = !discoverabilityKnown;
        }
        setStatus(String(nearbyEvent.message || 'Nearby 服务发生错误'), 'error');
        break;
      default:
        break;
    }
  };

  const disposeEvent = bridge?.onNearbyEvent?.(onEvent) ?? (() => undefined);

  discoverabilityToggle.addEventListener('change', () => {
    const enabled = discoverabilityToggle.checked;
    pendingDiscoverability = enabled;
    discoverabilityToggle.disabled = true;
    void Promise.resolve(bridge?.nearbyCommand?.({ type: 'set_discoverable', enabled }))
      .catch((error: unknown) => {
        discoverabilityToggle.checked = !enabled;
        pendingDiscoverability = null;
        discoverabilityToggle.disabled = !discoverabilityKnown;
        setStatus(`更新发现设置失败：${error instanceof Error ? error.message : String(error)}`, 'error');
      });
  });

  createRoom.addEventListener('click', () => {
    const selectedPeers = selectedConnectedPeers();
    if (!selectedPeers.length) return;
    setStatus('正在创建群聊…');
    void bridge?.nearbyCommand?.({
      type: 'create_room',
      room_id: makeRoomId(),
      room_name: roomName.value.trim() || '同伴群聊',
      peer_ids: selectedPeers.map((peer) => peer.peer_id),
    }).catch((error: unknown) => setStatus(`创建群聊失败：${error instanceof Error ? error.message : String(error)}`, 'error'));
  });

  mentionButton.addEventListener('click', () => {
    renderMentionMenu();
    mentionMenu.hidden = !mentionMenu.hidden;
  });

  cancelReply.addEventListener('click', () => setReplyTarget(null));

  roomForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = messageInput.value.trim();
    if (!text || !currentRoom) return;
    const mentions = [...new Set([
      ...selectedMentions,
      ...mentionsInText(text, roomPeerList(), localPeerId),
    ])];
    const reply = replyTarget;
    messageInput.value = '';
    selectedMentions.clear();
    setReplyTarget(null);
    mentionMenu.hidden = true;
    void bridge?.nearbyCommand?.({
      type: 'send_room_message',
      room_id: currentRoom.roomId,
      text,
      mentions,
      ...(reply ? { reply_to: reply } : {}),
    }).catch((error: unknown) => {
      setStatus(`发送失败：${error instanceof Error ? error.message : String(error)}`, 'error');
    });
  });

  fileButton.addEventListener('click', () => {
    if (!currentRoom || !bridge?.nearbySelectFile) return;
    setStatus('正在选择文件…');
    void bridge.nearbySelectFile().then((file: NearbyFileSelection | null) => {
      if (!file || !currentRoom) return;
      const mentions = [...new Set([
        ...selectedMentions,
        ...mentionsInText(messageInput.value, roomPeerList(), localPeerId),
      ])];
      const reply = replyTarget;
      messageInput.value = '';
      selectedMentions.clear();
      setReplyTarget(null);
      void bridge.nearbyCommand?.({
        type: 'send_room_file',
        room_id: currentRoom.roomId,
        file_id: file.file_id,
        name: file.name,
        mime_type: file.mime_type,
        size: file.size,
        sha256: file.sha256,
        data_base64: file.data_base64,
        mentions,
        ...(reply ? { reply_to: reply } : {}),
      }).then(() => setStatus(`正在发送文件：${file.name}`)).catch((error: unknown) => {
        setStatus(`文件发送失败：${error instanceof Error ? error.message : String(error)}`, 'error');
      });
    }).catch((error: unknown) => setStatus(`文件选择失败：${error instanceof Error ? error.message : String(error)}`, 'error'));
  });

  leaveRoom.addEventListener('click', () => {
    if (!currentRoom) return;
    void bridge?.nearbyCommand?.({ type: 'leave_room', room_id: currentRoom.roomId });
  });

  return {
    activate(): void {
      active = true;
      void bridge?.nearbyStart?.().catch((error: unknown) => setStatus(`Nearby 启动失败：${error instanceof Error ? error.message : String(error)}`, 'error'));
    },
    dispose(): void {
      const wasActive = active;
      active = false;
      disposeEvent();
      if (currentRoom) void bridge?.nearbyCommand?.({ type: 'leave_room', room_id: currentRoom.roomId });
      if (wasActive) void bridge?.nearbyStop?.();
      root.replaceChildren();
    },
  };
}
