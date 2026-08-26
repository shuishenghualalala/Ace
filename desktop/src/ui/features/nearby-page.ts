interface NearbyPeer {
  peer_id: string;
  display_name: string;
  agent_name: string;
  capabilities: string[];
  connection: 'discovered' | 'connecting' | 'connected' | 'disconnected' | 'unavailable';
}

interface NearbyMessage {
  message_id: string;
  sender: string;
  message_type: string;
  payload: { text?: string };
}

interface NearbyEvent {
  type: string;
  peer?: NearbyPeer;
  peer_id?: string;
  discoverable?: boolean;
  message?: NearbyMessage | string;
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

function peerLabel(peer: NearbyPeer | undefined, fallback = 'Ace Agent'): string {
  return peer?.display_name?.trim() || peer?.agent_name?.trim() || fallback;
}

function peerInitial(peer: NearbyPeer | undefined): string {
  return peerLabel(peer, '?').slice(0, 1).toLocaleUpperCase();
}

function connectionLabel(connection: NearbyPeer['connection']): string {
  switch (connection) {
    case 'connected': return '已连接';
    case 'connecting': return '正在连接';
    case 'disconnected': return '连接已断开';
    case 'unavailable': return '已离开';
    default: return '可连接';
  }
}

function messageTime(): string {
  return new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date());
}

export interface NearbyPage {
  activate(): void;
  dispose(): void;
}

export function mountNearbyPage(root: HTMLElement, bridge: Window['Crew'] = window.Crew): NearbyPage {
  const peers = new Map<string, NearbyPeer>();
  const messages = new Map<string, NearbyMessage[]>();
  let localPeerId = '';
  let activePeerId: string | null = null;
  let active = false;
  let discovering = true;
  let discoverabilityKnown = false;
  let pendingDiscoverability: boolean | null = null;

  const page = document.createElement('div');
  page.className = 'nearby-page';

  const header = document.createElement('header');
  header.className = 'nearby-page__header';
  const heading = document.createElement('div');
  heading.className = 'nearby-page__heading';
  heading.append(
    textElement('h1', 'nearby-page__title', '同伴'),
    textElement('p', 'nearby-page__subtitle', '通过蓝牙发现附近的 Ace，然后与对方的 Agent 对话。'),
  );
  const headerActions = document.createElement('div');
  headerActions.className = 'nearby-page__header-actions';
  const status = textElement('span', 'nearby-page__status', '正在准备…');
  const privacyToggle = document.createElement('input');
  privacyToggle.type = 'checkbox';
  privacyToggle.className = 'nearby-privacy__toggle';
  privacyToggle.checked = true;
  privacyToggle.disabled = true;
  privacyToggle.setAttribute('aria-label', '允许附近的 Ace 发现我');
  const privacyLabel = document.createElement('label');
  privacyLabel.className = 'nearby-privacy';
  privacyLabel.append(privacyToggle, textElement('span', 'nearby-privacy__text', '允许被发现'));
  headerActions.append(status, privacyLabel);
  header.append(heading, headerActions);

  const workspace = document.createElement('div');
  workspace.className = 'nearby-workspace';
  const discoveryPane = document.createElement('section');
  discoveryPane.className = 'nearby-discovery';
  const discoveryHeader = document.createElement('div');
  discoveryHeader.className = 'nearby-discovery__header';
  const discoveryCopy = document.createElement('div');
  discoveryCopy.append(
    textElement('h2', 'nearby-discovery__title', '附近的 Ace'),
    textElement('p', 'nearby-discovery__hint', '选择一台设备并连接'),
  );
  const scanButton = document.createElement('button');
  scanButton.type = 'button';
  scanButton.className = 'nearby-scan-button';
  scanButton.textContent = '停止查找';
  discoveryHeader.append(discoveryCopy, scanButton);

  const radar = document.createElement('div');
  radar.className = 'nearby-radar';
  radar.setAttribute('aria-hidden', 'true');
  radar.append(
    textElement('span', 'nearby-radar__ring nearby-radar__ring--outer', ''),
    textElement('span', 'nearby-radar__ring nearby-radar__ring--middle', ''),
    textElement('span', 'nearby-radar__ring nearby-radar__ring--inner', ''),
    textElement('span', 'nearby-radar__core', 'A'),
  );
  const peerList = document.createElement('div');
  peerList.className = 'nearby-peer-list';
  discoveryPane.append(discoveryHeader, radar, peerList);

  const conversationPane = document.createElement('section');
  conversationPane.className = 'nearby-conversation';
  const emptyState = document.createElement('div');
  emptyState.className = 'nearby-empty-state';
  emptyState.append(
    textElement('span', 'nearby-empty-state__symbol', '⌁'),
    textElement('h2', 'nearby-empty-state__title', '选择一个附近的 Ace'),
    textElement('p', 'nearby-empty-state__copy', '连接后，消息会通过蓝牙发送给对方 Ace Agent。'),
  );

  const conversation = document.createElement('div');
  conversation.className = 'nearby-chat';
  conversation.hidden = true;
  const chatHeader = document.createElement('header');
  chatHeader.className = 'nearby-chat__header';
  const chatIdentity = document.createElement('div');
  chatIdentity.className = 'nearby-chat__identity';
  const chatAvatar = textElement('span', 'nearby-chat__avatar', '?');
  const chatCopy = document.createElement('div');
  const chatName = textElement('h2', 'nearby-chat__name', 'Ace Agent');
  const chatState = textElement('p', 'nearby-chat__state', '尚未连接');
  chatCopy.append(chatName, chatState);
  chatIdentity.append(chatAvatar, chatCopy);
  const disconnectButton = document.createElement('button');
  disconnectButton.type = 'button';
  disconnectButton.className = 'nearby-secondary-action';
  disconnectButton.textContent = '断开';
  chatHeader.append(chatIdentity, disconnectButton);
  const messageList = document.createElement('div');
  messageList.className = 'nearby-chat__messages';
  messageList.setAttribute('aria-live', 'polite');
  const composer = document.createElement('form');
  composer.className = 'nearby-composer';
  const messageInput = document.createElement('textarea');
  messageInput.className = 'nearby-composer__input';
  messageInput.rows = 1;
  messageInput.maxLength = 8_000;
  messageInput.placeholder = '连接后即可发送消息';
  messageInput.disabled = true;
  const sendButton = document.createElement('button');
  sendButton.type = 'submit';
  sendButton.className = 'nearby-send-button';
  sendButton.textContent = '发送';
  sendButton.disabled = true;
  composer.append(messageInput, sendButton);
  conversation.append(chatHeader, messageList, composer);
  conversationPane.append(emptyState, conversation);
  workspace.append(discoveryPane, conversationPane);
  page.append(header, workspace);
  root.replaceChildren(page);

  const setStatus = (text: string, tone: 'normal' | 'error' = 'normal'): void => {
    status.textContent = text;
    status.dataset.tone = tone;
  };

  const updateComposer = (): void => {
    const peer = activePeerId ? peers.get(activePeerId) : undefined;
    const connected = peer?.connection === 'connected';
    messageInput.disabled = !connected;
    messageInput.placeholder = connected ? `发消息给 ${peerLabel(peer)} 的 Agent` : '连接后即可发送消息';
    sendButton.disabled = !connected || messageInput.value.trim().length === 0;
  };

  const renderMessages = (): void => {
    messageList.replaceChildren();
    if (!activePeerId) return;
    const items = messages.get(activePeerId) ?? [];
    if (items.length === 0) {
      messageList.append(textElement('p', 'nearby-chat__messages-empty', '连接已准备好，发一条消息开始交流。'));
      return;
    }
    for (const message of items) {
      const own = message.sender === localPeerId;
      const item = document.createElement('article');
      const failed = message.message_type === 'agent.error';
      item.className = `nearby-message${own ? ' nearby-message--own' : ''}${failed ? ' nearby-message--error' : ''}`;
      item.dataset.messageId = message.message_id;
      const bubble = textElement('p', 'nearby-message__bubble', String(message.payload.text ?? ''));
      const time = textElement('time', 'nearby-message__time', messageTime());
      item.append(bubble, time);
      messageList.append(item);
    }
    messageList.scrollTop = messageList.scrollHeight;
  };

  const renderConversation = (): void => {
    const peer = activePeerId ? peers.get(activePeerId) : undefined;
    emptyState.hidden = Boolean(peer);
    conversation.hidden = !peer;
    if (!peer) return;
    chatAvatar.textContent = peerInitial(peer);
    chatName.textContent = peerLabel(peer);
    chatState.textContent = `${peer.agent_name || 'Ace Agent'} · ${connectionLabel(peer.connection)}`;
    chatState.dataset.connection = peer.connection;
    disconnectButton.hidden = peer.connection !== 'connected';
    renderMessages();
    updateComposer();
  };

  const connectPeer = (peerId: string): void => {
    const peer = peers.get(peerId);
    if (!peer || peer.connection === 'connecting' || peer.connection === 'unavailable') return;
    activePeerId = peerId;
    peers.set(peerId, { ...peer, connection: 'connecting' });
    renderPeers();
    renderConversation();
    setStatus(`正在连接 ${peerLabel(peer)}…`);
    void bridge?.nearbyCommand?.({ type: 'connect_peer', peer_id: peerId })
      .catch((error: unknown) => {
        peers.set(peerId, { ...peer, connection: 'discovered' });
        renderPeers();
        renderConversation();
        setStatus(`连接失败：${error instanceof Error ? error.message : String(error)}`, 'error');
      });
  };

  function renderPeers(): void {
    peerList.replaceChildren();
    const availablePeers = [...peers.values()];
    if (availablePeers.length === 0) {
      peerList.append(textElement(
        'p',
        'nearby-peer-list__empty',
        discovering ? '正在通过蓝牙寻找附近的 Ace…' : '查找已暂停',
      ));
      return;
    }
    for (const peer of availablePeers) {
      const card = document.createElement('article');
      card.className = `nearby-peer-card${peer.peer_id === activePeerId ? ' nearby-peer-card--active' : ''}`;
      card.dataset.connection = peer.connection;
      card.addEventListener('click', () => {
        activePeerId = peer.peer_id;
        renderPeers();
        renderConversation();
      });
      const avatar = textElement('span', 'nearby-peer-card__avatar', peerInitial(peer));
      const copy = document.createElement('span');
      copy.className = 'nearby-peer-card__copy';
      copy.append(
        textElement('strong', 'nearby-peer-card__name', peerLabel(peer)),
        textElement('span', 'nearby-peer-card__agent', peer.agent_name || 'Ace Agent'),
        textElement('span', 'nearby-peer-card__connection', connectionLabel(peer.connection)),
      );
      const action = document.createElement('button');
      action.type = 'button';
      action.className = 'nearby-peer-card__action';
      action.textContent = peer.connection === 'connected'
        ? '打开'
        : peer.connection === 'connecting'
          ? '连接中'
          : peer.connection === 'unavailable'
            ? '不可用'
            : '连接';
      action.addEventListener('click', (event) => {
        event.stopPropagation();
        if (peer.connection === 'connected') {
          activePeerId = peer.peer_id;
          renderPeers();
          renderConversation();
        } else {
          connectPeer(peer.peer_id);
        }
      });
      card.append(avatar, copy, action);
      peerList.append(card);
    }
  }

  const onEvent = (event: { type: string; [key: string]: unknown }): void => {
    const nearbyEvent = event as NearbyEvent;
    switch (nearbyEvent.type) {
      case 'ready': {
        if (nearbyEvent.peer) localPeerId = nearbyEvent.peer.peer_id;
        privacyToggle.checked = nearbyEvent.discoverable !== false;
        privacyToggle.disabled = false;
        discoverabilityKnown = true;
        setStatus(nearbyEvent.discoverable === false ? '仅查找附近 Ace' : '正在查找附近 Ace');
        break;
      }
      case 'discovery_started':
        discovering = true;
        radar.dataset.scanning = 'true';
        scanButton.textContent = '停止查找';
        setStatus('正在查找附近 Ace');
        renderPeers();
        break;
      case 'discovery_stopped':
        discovering = false;
        radar.dataset.scanning = 'false';
        scanButton.textContent = '重新查找';
        setStatus('查找已暂停');
        renderPeers();
        break;
      case 'discoverability_changed':
        privacyToggle.checked = nearbyEvent.discoverable !== false;
        privacyToggle.disabled = false;
        pendingDiscoverability = null;
        setStatus(nearbyEvent.discoverable === false ? '已对附近设备隐藏' : '附近设备可以发现你');
        break;
      case 'peer_discovered': {
        const peer = nearbyEvent.peer;
        if (!peer || peer.peer_id === localPeerId) break;
        const current = peers.get(peer.peer_id);
        peers.set(peer.peer_id, {
          ...peer,
          connection: current?.connection === 'connected' || current?.connection === 'connecting'
            ? current.connection
            : 'discovered',
        });
        renderPeers();
        renderConversation();
        break;
      }
      case 'peer_connected': {
        const peer = nearbyEvent.peer;
        if (!peer) break;
        peers.set(peer.peer_id, { ...peer, connection: 'connected' });
        activePeerId = peer.peer_id;
        setStatus(`已连接 ${peerLabel(peer)}`);
        renderPeers();
        renderConversation();
        messageInput.focus();
        break;
      }
      case 'peer_disconnected': {
        const peerId = String(nearbyEvent.peer_id || '');
        const peer = peers.get(peerId);
        if (peer) peers.set(peerId, { ...peer, connection: 'disconnected' });
        setStatus(peer ? `与 ${peerLabel(peer)} 的连接已断开` : '连接已断开');
        renderPeers();
        renderConversation();
        break;
      }
      case 'peer_unavailable': {
        const peerId = String(nearbyEvent.peer_id || '');
        const peer = peers.get(peerId);
        if (peer && peer.connection !== 'connected') peers.set(peerId, { ...peer, connection: 'unavailable' });
        renderPeers();
        renderConversation();
        break;
      }
      case 'peer_connection_failed': {
        const peerId = String(nearbyEvent.peer_id || '');
        const peer = peers.get(peerId);
        if (peer) peers.set(peerId, { ...peer, connection: 'discovered' });
        setStatus(String(nearbyEvent.message || '连接失败'), 'error');
        renderPeers();
        renderConversation();
        break;
      }
      case 'message': {
        const peerId = String(nearbyEvent.peer_id || '');
        const message = typeof nearbyEvent.message === 'object' ? nearbyEvent.message as NearbyMessage : undefined;
        if (
          !peerId
          || !message
          || !['agent.request', 'agent.response', 'agent.error'].includes(message.message_type)
        ) break;
        const history = messages.get(peerId) ?? [];
        if (!history.some((item) => item.message_id === message.message_id)) history.push(message);
        messages.set(peerId, history);
        if (!activePeerId || message.sender !== localPeerId) activePeerId = peerId;
        renderPeers();
        renderConversation();
        if (message.message_type === 'agent.error') {
          setStatus(`${peerLabel(peers.get(peerId))} 的 Agent 回复失败`, 'error');
        } else if (message.message_type === 'agent.request') {
          setStatus(
            message.sender === localPeerId
              ? '消息已发送，等待对方 Agent 回复…'
              : `正在由本机 Agent 回复 ${peerLabel(peers.get(peerId))}…`,
          );
        } else {
          setStatus(
            message.sender === localPeerId
              ? `本机 Agent 已回复 ${peerLabel(peers.get(peerId))}`
              : `收到 ${peerLabel(peers.get(peerId))} Agent 的回复`,
          );
        }
        break;
      }
      case 'error':
        if (pendingDiscoverability !== null) {
          privacyToggle.checked = !pendingDiscoverability;
          pendingDiscoverability = null;
          privacyToggle.disabled = !discoverabilityKnown;
        }
        setStatus(String(nearbyEvent.message || '同伴服务发生错误'), 'error');
        break;
      default: break;
    }
  };

  const disposeEvent = bridge?.onNearbyEvent?.(onEvent) ?? (() => undefined);
  privacyToggle.addEventListener('change', () => {
    const enabled = privacyToggle.checked;
    pendingDiscoverability = enabled;
    privacyToggle.disabled = true;
    void Promise.resolve(bridge?.nearbyCommand?.({ type: 'set_discoverable', enabled }))
      .catch((error: unknown) => {
        privacyToggle.checked = !enabled;
        privacyToggle.disabled = !discoverabilityKnown;
        pendingDiscoverability = null;
        setStatus(`更新发现设置失败：${error instanceof Error ? error.message : String(error)}`, 'error');
      });
  });
  scanButton.addEventListener('click', () => {
    void bridge?.nearbyCommand?.({ type: discovering ? 'stop_discovery' : 'start_discovery' });
  });
  disconnectButton.addEventListener('click', () => {
    if (!activePeerId) return;
    void bridge?.nearbyCommand?.({ type: 'disconnect_peer', peer_id: activePeerId });
  });
  messageInput.addEventListener('input', updateComposer);
  messageInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });
  composer.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = messageInput.value.trim();
    const peer = activePeerId ? peers.get(activePeerId) : undefined;
    if (!text || !peer || peer.connection !== 'connected') return;
    messageInput.value = '';
    updateComposer();
    setStatus('正在发送…');
    void bridge?.nearbyCommand?.({ type: 'send_agent_request', peer_id: peer.peer_id, text })
      .catch((error: unknown) => setStatus(`发送失败：${error instanceof Error ? error.message : String(error)}`, 'error'));
  });

  radar.dataset.scanning = 'true';
  renderPeers();
  renderConversation();

  return {
    activate(): void {
      active = true;
      void bridge?.nearbyStart?.()
        .catch((error: unknown) => setStatus(`同伴服务启动失败：${error instanceof Error ? error.message : String(error)}`, 'error'));
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
