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
  messages: NearbyMessage[];
  unreadCount: number;
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
  messages?: NearbyMessage[];
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

function memberInitial(peer: NearbyPeer | undefined, peerId: string): string {
  return memberLabel(peer, peerId).trim().slice(0, 1).toLocaleUpperCase() || '?';
}

function formatMessageTime(): string {
  return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(new Date());
}

function roomPreview(room: NearbyRoom): string {
  const message = room.messages.at(-1);
  if (!message) return '还没有消息，开始聊天吧';
  if (message.payload.text) return message.payload.text;
  if (message.payload.file) return `文件：${message.payload.file.name}`;
  return '新消息';
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
  const headingCopy = document.createElement('div');
  headingCopy.className = 'nearby-page__heading-copy';
  headingCopy.append(
    textElement('h1', 'nearby-page__title', '同伴'),
    textElement('p', 'nearby-page__subtitle', '发现附近的 Agent，一起工作、讨论和分享文件。'),
  );
  const headerActions = document.createElement('div');
  headerActions.className = 'nearby-page__header-actions';
  const status = textElement('span', 'nearby-page__status', '准备发现');
  headerActions.append(status);
  const privacyToggle = document.createElement('input');
  privacyToggle.type = 'checkbox';
  privacyToggle.className = 'nearby-privacy__toggle';
  privacyToggle.checked = true;
  privacyToggle.disabled = true;
  const privacyLabel = document.createElement('label');
  privacyLabel.className = 'nearby-privacy__label';
  privacyLabel.append(
    privacyToggle,
    textElement('span', 'nearby-privacy__title', '允许被发现'),
  );
  headerActions.append(privacyLabel);
  header.append(headingCopy, headerActions);

  const workspace = document.createElement('div');
  workspace.className = 'nearby-workspace';

  const sidebar = document.createElement('aside');
  sidebar.className = 'nearby-sidebar';
  const roomListHeader = document.createElement('div');
  roomListHeader.className = 'nearby-sidebar__header';
  roomListHeader.append(textElement('h2', 'nearby-sidebar__title', '群聊'));
  const newRoomButton = document.createElement('button');
  newRoomButton.type = 'button';
  newRoomButton.className = 'nearby-icon-action';
  newRoomButton.textContent = '+';
  newRoomButton.title = '创建群聊';
  newRoomButton.setAttribute('aria-label', '创建群聊');
  roomListHeader.append(newRoomButton);
  const roomList = document.createElement('div');
  roomList.className = 'nearby-room-list';
  const roomListPanel = document.createElement('section');
  roomListPanel.className = 'nearby-room-list-panel';
  roomListPanel.append(roomListHeader, roomList);

  const discovery = document.createElement('section');
  discovery.className = 'nearby-discovery';
  const discoveryHeader = document.createElement('div');
  discoveryHeader.className = 'nearby-section__header';
  discoveryHeader.append(
    textElement('h2', 'nearby-section__title', '附近 Agent'),
    textElement('span', 'nearby-section__hint', 'BLE'),
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
  createRoom.textContent = '创建群聊';
  createRoom.disabled = true;
  const discoveryActions = document.createElement('div');
  discoveryActions.className = 'nearby-discovery__actions';
  discoveryActions.append(roomName, createRoom);
  discovery.append(discoveryHeader, peerList, discoveryActions);
  sidebar.append(roomListPanel, discovery);

  const chatShell = document.createElement('main');
  chatShell.className = 'nearby-chat-shell';
  const emptyState = document.createElement('section');
  emptyState.className = 'nearby-empty-state';
  emptyState.append(
    textElement('span', 'nearby-empty-state__mark', '✦'),
    textElement('h2', 'nearby-empty-state__title', '选择一个群聊'),
    textElement('p', 'nearby-empty-state__copy', '从左侧打开已有群聊，或在附近 Agent 中选择成员创建新的群聊。'),
  );

  const room = document.createElement('section');
  room.className = 'nearby-room';
  room.hidden = true;
  const roomHeader = document.createElement('header');
  roomHeader.className = 'nearby-room__header';
  const roomHeading = document.createElement('div');
  roomHeading.className = 'nearby-room__heading';
  const roomAvatar = textElement('span', 'nearby-room__avatar', '群');
  const roomHeadingCopy = document.createElement('div');
  roomHeadingCopy.className = 'nearby-room__heading-copy';
  const roomTitle = textElement('h2', 'nearby-room__title', '同伴群聊');
  const roomMembers = textElement('p', 'nearby-room__members', '');
  roomHeadingCopy.append(roomTitle, roomMembers);
  roomHeading.append(roomAvatar, roomHeadingCopy);
  const roomHeaderActions = document.createElement('div');
  roomHeaderActions.className = 'nearby-room__header-actions';
  const memberToggle = document.createElement('button');
  memberToggle.type = 'button';
  memberToggle.className = 'nearby-secondary-action';
  memberToggle.textContent = '成员';
  memberToggle.setAttribute('aria-expanded', 'false');
  const leaveRoom = document.createElement('button');
  leaveRoom.type = 'button';
  leaveRoom.className = 'nearby-secondary-action nearby-secondary-action--danger';
  leaveRoom.textContent = '退出';
  roomHeaderActions.append(memberToggle, leaveRoom);
  roomHeader.append(roomHeading, roomHeaderActions);

  const memberPanel = document.createElement('div');
  memberPanel.className = 'nearby-member-panel';
  memberPanel.hidden = true;
  const memberList = document.createElement('div');
  memberList.className = 'nearby-member-panel__list';
  memberPanel.append(memberList);
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
  const composer = document.createElement('div');
  composer.className = 'nearby-composer';
  const composerActions = document.createElement('div');
  composerActions.className = 'nearby-composer__actions';
  const mentionButton = document.createElement('button');
  mentionButton.type = 'button';
  mentionButton.className = 'nearby-composer-action';
  mentionButton.textContent = '@';
  mentionButton.title = '提及成员';
  const fileButton = document.createElement('button');
  fileButton.type = 'button';
  fileButton.className = 'nearby-composer-action';
  fileButton.textContent = '＋';
  fileButton.title = '发送文件';
  composerActions.append(mentionButton, fileButton);
  const messageInput = document.createElement('input');
  messageInput.type = 'text';
  messageInput.className = 'nearby-room__input';
  messageInput.placeholder = '输入消息…';
  messageInput.autocomplete = 'off';
  const sendMessage = document.createElement('button');
  sendMessage.type = 'submit';
  sendMessage.className = 'nearby-primary-action nearby-send-action';
  sendMessage.textContent = '发送';
  sendMessage.disabled = true;
  const mentionMenu = document.createElement('div');
  mentionMenu.className = 'nearby-mention-menu';
  mentionMenu.hidden = true;
  composer.append(composerActions, messageInput, sendMessage, mentionMenu);
  roomForm.append(replyBar, composer);
  room.append(roomHeader, memberPanel, roomMessages, roomForm);
  chatShell.append(emptyState, room);
  workspace.append(sidebar, chatShell);
  page.append(header, workspace);
  root.replaceChildren(page);

  const peers = new Map<string, NearbyPeer>();
  const selected = new Set<string>();
  const selectedMentions = new Set<string>();
  const rooms = new Map<string, NearbyRoom>();
  const fileTransfers = new Map<string, { message: NearbyMessage; chunks: Array<string | undefined> }>();
  let activeRoomId: string | null = null;
  let replyTarget: NearbyReplyReference | null = null;
  let localPeerId = '';
  let active = false;
  let discoverabilityKnown = false;
  let pendingDiscoverability: boolean | null = null;

  const currentRoom = (): NearbyRoom | null => activeRoomId ? rooms.get(activeRoomId) ?? null : null;
  const setStatus = (value: string, tone: 'normal' | 'error' = 'normal'): void => {
    status.textContent = value;
    status.dataset.tone = tone;
  };

  const selectedConnectedPeers = (): NearbyPeer[] => Array.from(selected)
    .map((peerId) => peers.get(peerId))
    .filter((peer): peer is NearbyPeer => Boolean(peer && peer.connection === 'connected'));

  const updateComposer = (): void => {
    const enabled = Boolean(currentRoom());
    const hasText = messageInput.value.trim().length > 0;
    messageInput.disabled = !enabled;
    mentionButton.disabled = !enabled;
    fileButton.disabled = !enabled;
    sendMessage.disabled = !enabled || !hasText;
  };

  const roomPeerList = (): NearbyPeer[] => currentRoom()?.peerIds
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
      const avatar = textElement('span', 'nearby-peer-card__avatar', memberInitial(peer, peer.peer_id));
      const copy = document.createElement('span');
      copy.className = 'nearby-peer-card__copy';
      copy.append(
        textElement('strong', 'nearby-peer-card__name', peer.display_name || peer.peer_id),
        textElement('span', 'nearby-peer-card__agent', peer.agent_name || 'Crew Agent'),
        textElement('span', 'nearby-peer-card__capabilities', peer.capabilities?.join(' · ') || '可进行群聊'),
        textElement('span', 'nearby-peer-card__workspace', '对方本机私有'),
      );
      const connection = textElement('span', 'nearby-peer-card__connection', peer.connection === 'connected' ? '在线' : peer.connection === 'disconnected' ? '已断开' : '连接中');
      label.append(checkbox, avatar, copy, connection);
      peerList.append(label);
    }
    createRoom.disabled = selectedConnectedPeers().length === 0;
  };

  const renderRoomList = (): void => {
    roomList.replaceChildren();
    if (rooms.size === 0) {
      roomList.append(textElement('p', 'nearby-room-list__empty', '还没有群聊'));
      return;
    }
    for (const roomItem of rooms.values()) {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = `nearby-room-list__item${roomItem.roomId === activeRoomId ? ' nearby-room-list__item--active' : ''}`;
      const firstMember = roomItem.peerIds.find((peerId) => peerId !== localPeerId) ?? roomItem.peerIds[0] ?? '群';
      const copy = document.createElement('span');
      copy.className = 'nearby-room-list__copy';
      copy.append(
        textElement('strong', 'nearby-room-list__name', roomItem.roomName),
        textElement('span', 'nearby-room-list__preview', roomPreview(roomItem)),
      );
      const meta = document.createElement('span');
      meta.className = 'nearby-room-list__meta';
      meta.append(
        textElement('span', 'nearby-room-list__time', roomItem.messages.length ? formatMessageTime() : ''),
        ...(roomItem.unreadCount ? [textElement('span', 'nearby-room-list__unread', String(roomItem.unreadCount))] : []),
      );
      item.append(textElement('span', 'nearby-room-list__avatar', memberInitial(peers.get(firstMember), firstMember)), copy, meta);
      item.setAttribute('aria-label', `打开群聊 ${roomItem.roomName}`);
      item.addEventListener('click', () => {
        activeRoomId = roomItem.roomId;
        roomItem.unreadCount = 0;
        renderRoom();
      });
      roomList.append(item);
    }
  };

  const renderMembers = (): void => {
    memberList.replaceChildren();
    for (const peerId of currentRoom()?.peerIds ?? []) {
      const peer = peers.get(peerId);
      const local = peerId === localPeerId;
      const connected = local || peer?.connection === 'connected';
      const item = document.createElement('div');
      item.className = 'nearby-member-panel__item';
      item.dataset.connection = connected ? 'connected' : 'disconnected';
      item.append(
        textElement('span', 'nearby-member-panel__avatar', memberInitial(peer, local ? '我' : peerId)),
        textElement('span', 'nearby-member-panel__name', local ? '我' : memberLabel(peer, peerId)),
        textElement('span', 'nearby-member-panel__status', local ? '我' : connected ? '在线' : '已断开'),
      );
      memberList.append(item);
    }
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
        const label = memberLabel(peer, peer.peer_id);
        messageInput.value = `${messageInput.value.slice(0, start)}@${label} ${messageInput.value.slice(end)}`;
        messageInput.selectionStart = messageInput.selectionEnd = start + label.length + 2;
        selectedMentions.add(peer.peer_id);
        mentionMenu.hidden = true;
        updateComposer();
        messageInput.focus();
      });
      mentionMenu.append(option);
    }
    if (!mentionMenu.childElementCount) mentionMenu.append(textElement('span', 'nearby-mention-menu__empty', '当前房间没有可提及的成员'));
  };

  const renderMessage = (message: NearbyMessage): HTMLElement => {
    const own = message.sender === localPeerId;
    const item = document.createElement('article');
    item.className = `nearby-message${own ? ' nearby-message--own' : ''}`;
    item.dataset.messageId = message.message_id;
    const identity = document.createElement('div');
    identity.className = 'nearby-message__identity';
    identity.append(
      textElement('span', 'nearby-message__avatar', memberInitial(peers.get(message.sender), own ? '我' : message.sender)),
      textElement('span', 'nearby-message__sender', own ? '我' : memberLabel(peers.get(message.sender), message.sender)),
      textElement('time', 'nearby-message__time', formatMessageTime()),
    );
    item.append(identity);
    const reply = message.payload.reply_to;
    if (reply) item.append(textElement('div', 'nearby-message__reply', `回复 ${memberLabel(peers.get(reply.sender), reply.sender)}：${reply.text}`));
    if (message.payload.text) item.append(textElement('p', 'nearby-message__text', message.payload.text));
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
        void bridge?.nearbySaveFile?.({ name: file.name, mime_type: file.mime_type, size: file.size, sha256: file.sha256, data_base64: file.data_base64 })
          .then((result) => { if (result?.ok) setStatus(`已保存：${file.name}`); })
          .catch((error: unknown) => setStatus(`保存失败：${error instanceof Error ? error.message : String(error)}`, 'error'));
      });
      fileCard.append(save);
      item.append(fileCard);
    }
    const mentionIds = message.payload.mentions ?? [];
    if (mentionIds.length) item.append(textElement('span', 'nearby-message__mentions', `提及 ${mentionIds.map((id) => `@${memberLabel(peers.get(id), id)}`).join('、')}`));
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
    const activeRoom = currentRoom();
    if (!activeRoom || activeRoom.messages.length === 0) {
      roomMessages.append(textElement('p', 'nearby-room__messages-empty', '群聊已准备好，发一条消息开始交流。'));
    } else {
      for (const message of activeRoom.messages) roomMessages.append(renderMessage(message));
    }
    roomMessages.scrollTop = roomMessages.scrollHeight;
  };

  const renderRoom = (): void => {
    const activeRoom = currentRoom();
    emptyState.hidden = Boolean(activeRoom);
    room.hidden = !activeRoom;
    renderRoomList();
    if (!activeRoom) {
      updateComposer();
      return;
    }
    roomTitle.textContent = activeRoom.roomName;
    const firstMember = activeRoom.peerIds.find((peerId) => peerId !== localPeerId) ?? activeRoom.peerIds[0] ?? '群';
    roomAvatar.textContent = memberInitial(peers.get(firstMember), firstMember);
    const names = activeRoom.peerIds.map((peerId) => peerId === localPeerId ? '我' : memberLabel(peers.get(peerId), peerId));
    roomMembers.textContent = `${activeRoom.peerIds.length} 位成员 · ${names.join('、')}`;
    renderMembers();
    renderMentionMenu();
    renderMessages();
    updateComposer();
  };

  const setReplyTarget = (message: NearbyMessage | null): void => {
    if (!message) {
      replyTarget = null;
      replyBar.hidden = true;
      replyLabel.textContent = '';
      return;
    }
    const text = message.payload.text || message.payload.file?.name || '';
    replyTarget = { message_id: message.message_id, sender: message.sender, text: text.slice(0, 500) };
    replyLabel.textContent = `回复 ${memberLabel(peers.get(message.sender), message.sender)}：${text.slice(0, 100)}`;
    replyBar.hidden = false;
    messageInput.focus();
  };

  const appendMessage = (roomId: string, message: NearbyMessage): void => {
    const roomItem = rooms.get(roomId);
    if (!roomItem || roomItem.messages.some((candidate) => candidate.message_id === message.message_id)) return;
    roomItem.messages.push(message);
    if (roomId !== activeRoomId) roomItem.unreadCount += 1;
    renderRoomList();
    if (roomId === activeRoomId) renderMessages();
  };

  const receiveFileChunk = (message: NearbyMessage): void => {
    const file = message.payload.file;
    const roomId = message.payload.room_id;
    if (!file || !roomId || file.chunk_total <= 0 || file.chunk_index < 0 || file.chunk_index >= file.chunk_total) return;
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
    appendMessage(roomId, {
      ...transfer.message,
      message_id: `file:${file.file_id}`,
      payload: { ...transfer.message.payload, file: { ...file, data_base64: transfer.chunks.join('') } },
    });
    fileTransfers.delete(file.file_id);
    setStatus(`收到文件：${file.name}`);
  };

  const onEvent = (event: { type: string; [key: string]: unknown }): void => {
    const nearbyEvent = event as NearbyEvent;
    switch (nearbyEvent.type) {
      case 'ready': {
        const peer = nearbyEvent.peer;
        if (peer) localPeerId = peer.peer_id;
        privacyToggle.checked = nearbyEvent.discoverable !== false;
        privacyToggle.disabled = false;
        discoverabilityKnown = true;
        setStatus(nearbyEvent.discoverable === false ? '仅扫描附近 Agent' : '正在发现附近 Agent');
        break;
      }
      case 'discovery_started': setStatus('正在发现附近 Agent'); break;
      case 'discovery_stopped': setStatus('发现已暂停'); break;
      case 'discoverability_changed':
        privacyToggle.checked = nearbyEvent.discoverable !== false;
        privacyToggle.disabled = false;
        discoverabilityKnown = true;
        pendingDiscoverability = null;
        setStatus(nearbyEvent.discoverable === false ? '已停止被附近设备发现' : '已允许附近设备发现');
        break;
      case 'peer_discovered':
      case 'peer_connected': {
        const peer = nearbyEvent.peer;
        if (!peer) break;
        peers.set(peer.peer_id, { ...peer, connection: nearbyEvent.type === 'peer_connected' ? 'connected' : (peers.get(peer.peer_id)?.connection ?? 'discovered') });
        if (nearbyEvent.type === 'peer_connected') setStatus('已发现可加入群聊的 Agent');
        renderPeers();
        renderMembers();
        renderRoomList();
        break;
      }
      case 'peer_disconnected': {
        const peerId = String(nearbyEvent.peer_id || '');
        const peer = peers.get(peerId);
        if (peer) peers.set(peerId, { ...peer, connection: 'disconnected' });
        selected.delete(peerId);
        renderPeers();
        renderMembers();
        break;
      }
      case 'room_created':
      case 'room_joined':
      case 'room_restored': {
        const roomId = String(nearbyEvent.room_id || '');
        if (!roomId) break;
        const existing = rooms.get(roomId);
        rooms.set(roomId, {
          roomId,
          roomName: String(nearbyEvent.room_name || existing?.roomName || '同伴群聊'),
          peerIds: Array.isArray(nearbyEvent.peer_ids) ? nearbyEvent.peer_ids.map(String) : existing?.peerIds ?? [],
          messages: Array.isArray(nearbyEvent.messages) ? nearbyEvent.messages as NearbyMessage[] : existing?.messages ?? [],
          unreadCount: existing?.unreadCount ?? 0,
        });
        if (!activeRoomId || nearbyEvent.type !== 'room_restored') activeRoomId = roomId;
        renderRoom();
        setStatus(nearbyEvent.type === 'room_restored' ? '群聊已恢复' : '群聊已连接');
        break;
      }
      case 'room_left': {
        const roomId = String(nearbyEvent.room_id || '');
        rooms.delete(roomId);
        if (activeRoomId === roomId) activeRoomId = rooms.keys().next().value ?? null;
        messagesReset();
        renderRoom();
        setStatus('已退出群聊');
        break;
      }
      case 'message': {
        const message = typeof nearbyEvent.message === 'object' ? nearbyEvent.message as NearbyMessage : undefined;
        const roomId = message?.payload?.room_id;
        if (!message || !roomId) break;
        if (message.message_type === 'room.file') receiveFileChunk(message);
        else if (message.message_type === 'room.message') appendMessage(roomId, message);
        break;
      }
      case 'error':
        if (pendingDiscoverability !== null) {
          privacyToggle.checked = !pendingDiscoverability;
          pendingDiscoverability = null;
          privacyToggle.disabled = !discoverabilityKnown;
        }
        setStatus(String(nearbyEvent.message || 'Nearby 服务发生错误'), 'error');
        break;
      default: break;
    }
  };

  const messagesReset = (): void => {
    fileTransfers.clear();
    selectedMentions.clear();
    setReplyTarget(null);
  };

  const disposeEvent = bridge?.onNearbyEvent?.(onEvent) ?? (() => undefined);
  privacyToggle.addEventListener('change', () => {
    const enabled = privacyToggle.checked;
    pendingDiscoverability = enabled;
    privacyToggle.disabled = true;
    void Promise.resolve(bridge?.nearbyCommand?.({ type: 'set_discoverable', enabled })).catch((error: unknown) => {
      privacyToggle.checked = !enabled;
      pendingDiscoverability = null;
      privacyToggle.disabled = !discoverabilityKnown;
      setStatus(`更新发现设置失败：${error instanceof Error ? error.message : String(error)}`, 'error');
    });
  });

  newRoomButton.addEventListener('click', () => {
    roomName.focus();
    setStatus('选择在线 Agent 创建群聊');
  });
  memberToggle.addEventListener('click', () => {
    const expanded = memberPanel.hidden;
    memberPanel.hidden = !expanded;
    memberToggle.setAttribute('aria-expanded', String(expanded));
    if (expanded) renderMembers();
  });
  messageInput.addEventListener('input', updateComposer);
  createRoom.addEventListener('click', () => {
    const selectedPeers = selectedConnectedPeers();
    if (!selectedPeers.length) return;
    setStatus('正在创建群聊…');
    void bridge?.nearbyCommand?.({ type: 'create_room', room_id: makeRoomId(), room_name: roomName.value.trim() || '同伴群聊', peer_ids: selectedPeers.map((peer) => peer.peer_id) })
      .catch((error: unknown) => setStatus(`创建群聊失败：${error instanceof Error ? error.message : String(error)}`, 'error'));
  });
  mentionButton.addEventListener('click', () => {
    renderMentionMenu();
    mentionMenu.hidden = !mentionMenu.hidden;
  });
  cancelReply.addEventListener('click', () => setReplyTarget(null));
  roomForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = messageInput.value.trim();
    const activeRoom = currentRoom();
    if (!text || !activeRoom) return;
    const mentions = [...new Set([...selectedMentions, ...mentionsInText(text, roomPeerList(), localPeerId)])];
    const reply = replyTarget;
    messageInput.value = '';
    selectedMentions.clear();
    setReplyTarget(null);
    mentionMenu.hidden = true;
    updateComposer();
    setStatus('正在发送…');
    void bridge?.nearbyCommand?.({ type: 'send_room_message', room_id: activeRoom.roomId, text, mentions, ...(reply ? { reply_to: reply } : {}) })
      .catch((error: unknown) => setStatus(`发送失败：${error instanceof Error ? error.message : String(error)}`, 'error'));
  });
  fileButton.addEventListener('click', () => {
    const activeRoom = currentRoom();
    if (!activeRoom || !bridge?.nearbySelectFile) return;
    setStatus('正在选择文件…');
    void bridge.nearbySelectFile().then((file: NearbyFileSelection | null) => {
      const latestRoom = currentRoom();
      if (!file || !latestRoom) return;
      const mentions = [...new Set([...selectedMentions, ...mentionsInText(messageInput.value, roomPeerList(), localPeerId)])];
      const reply = replyTarget;
      messageInput.value = '';
      selectedMentions.clear();
      setReplyTarget(null);
      void bridge.nearbyCommand?.({ type: 'send_room_file', room_id: latestRoom.roomId, file_id: file.file_id, name: file.name, mime_type: file.mime_type, size: file.size, sha256: file.sha256, data_base64: file.data_base64, mentions, ...(reply ? { reply_to: reply } : {}) })
        .then(() => setStatus(`正在发送文件：${file.name}`))
        .catch((error: unknown) => setStatus(`文件发送失败：${error instanceof Error ? error.message : String(error)}`, 'error'));
    }).catch((error: unknown) => setStatus(`文件选择失败：${error instanceof Error ? error.message : String(error)}`, 'error'));
  });
  leaveRoom.addEventListener('click', () => {
    const activeRoom = currentRoom();
    if (!activeRoom) return;
    void bridge?.nearbyCommand?.({ type: 'leave_room', room_id: activeRoom.roomId });
  });

  renderPeers();
  renderRoom();
  updateComposer();

  return {
    activate(): void {
      active = true;
      void bridge?.nearbyStart?.().catch((error: unknown) => setStatus(`Nearby 启动失败：${error instanceof Error ? error.message : String(error)}`, 'error'));
    },
    dispose(): void {
      const wasActive = active;
      active = false;
      disposeEvent();
      if (wasActive) void bridge?.nearbyStop?.();
      root.replaceChildren();
    },
  };
}
