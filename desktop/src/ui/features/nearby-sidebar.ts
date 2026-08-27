/**
 * 同伴页左栏：我的身份卡、附近的 Ace 发现面板（雷达 + 设备卡片）、会话列表，
 * 以及建群 / 邀请进群 / 同伴 Agent 设置三个弹层。
 */

import {
  AGENT_MODE_LABELS,
  dmConversationId,
  type NearbyAgentMode,
  type NearbyConversation,
  type NearbyPeer,
  type NearbyStore,
} from './nearby-store';
import type { NearbyActions } from './nearby-page';

export interface NearbySidebar {
  element: HTMLElement;
  render(): void;
  dispose(): void;
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

function peerInitial(label: string): string {
  return label.trim().slice(0, 1).toLocaleUpperCase() || '?';
}

function connectionLabel(connection: NearbyPeer['connection']): string {
  switch (connection) {
    case 'connected': return '已连接';
    case 'connecting': return '连接中';
    case 'disconnected': return '已断开';
    case 'unavailable': return '已离开';
    default: return '可连接';
  }
}

function conversationTime(timestamp: number): string {
  if (!timestamp) return '';
  const date = new Date(timestamp);
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  return new Intl.DateTimeFormat(undefined, sameDay
    ? { hour: '2-digit', minute: '2-digit' }
    : { month: '2-digit', day: '2-digit' }).format(date);
}

/** 渲染层没有获取 toolset 清单的直连通道时的兜底候选，与 crew.tools.registry 的 toolset 命名对齐。 */
const FALLBACK_TOOLSETS = ['web', 'file', 'terminal', 'browser', 'vision', 'memory', 'tasks', 'cron', 'skills'];

export function createNearbySidebar(deps: {
  store: NearbyStore;
  actions: NearbyActions;
}): NearbySidebar {
  const { store, actions } = deps;

  const element = document.createElement('aside');
  element.className = 'nearby-sidebar';

  // A. 我的身份卡
  const identity = document.createElement('div');
  identity.className = 'nearby-identity';
  identity.setAttribute('role', 'button');
  identity.tabIndex = 0;
  identity.title = '同伴 Agent 设置';
  const identityAvatar = textElement('span', 'nearby-identity__avatar', '?');
  const identityCopy = document.createElement('div');
  identityCopy.className = 'nearby-identity__copy';
  const identityName = textElement('strong', 'nearby-identity__name', '我');
  const identityAgent = textElement('span', 'nearby-identity__agent', 'Ace Agent');
  identityCopy.append(identityName, identityAgent);
  const identityStatus = textElement('span', 'nearby-identity__status', '未就绪');
  identity.append(identityAvatar, identityCopy, identityStatus);
  const privacyLabel = document.createElement('label');
  privacyLabel.className = 'nearby-privacy nearby-sidebar__privacy';
  const privacyToggle = document.createElement('input');
  privacyToggle.type = 'checkbox';
  privacyToggle.className = 'nearby-privacy__toggle';
  privacyToggle.checked = true;
  privacyToggle.disabled = true;
  privacyToggle.setAttribute('aria-label', '允许附近的 Ace 发现我');
  // Linux 运行时只支持主动连接，无法作为外围设备被发现后接受连接
  if (window.Crew?.runtimePlatform === 'linux') {
    privacyLabel.title = 'Linux 暂不支持被动连接：你可以主动发现并连接其他设备，但其他设备无法向你发起连接';
  }
  privacyLabel.append(privacyToggle, textElement('span', 'nearby-privacy__text', '允许被发现'));
  privacyLabel.addEventListener('click', (event) => event.stopPropagation());

  // B. 附近的 Ace（可折叠）
  const discoverySection = document.createElement('section');
  discoverySection.className = 'nearby-section';
  const discoveryHeader = document.createElement('button');
  discoveryHeader.type = 'button';
  discoveryHeader.className = 'nearby-section__header';
  const discoveryCaret = textElement('span', 'nearby-section__caret', '▾');
  discoveryHeader.append(
    discoveryCaret,
    textElement('span', 'nearby-section__title', '附近的 Ace'),
  );
  const scanButton = document.createElement('button');
  scanButton.type = 'button';
  scanButton.className = 'nearby-scan-button';
  scanButton.textContent = '停止查找';
  scanButton.addEventListener('click', (event) => {
    event.stopPropagation();
    actions.toggleDiscovery();
  });
  discoveryHeader.append(scanButton);
  const discoveryBody = document.createElement('div');
  discoveryBody.className = 'nearby-section__body';
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
  discoveryBody.append(radar, peerList);
  discoverySection.append(discoveryHeader, discoveryBody);
  let discoveryOpen = true;
  discoveryHeader.addEventListener('click', () => {
    discoveryOpen = !discoveryOpen;
    discoveryBody.hidden = !discoveryOpen;
    discoveryCaret.textContent = discoveryOpen ? '▾' : '▸';
  });

  // C. 会话列表
  const conversationSection = document.createElement('section');
  conversationSection.className = 'nearby-section nearby-section--conversations';
  const conversationHeader = document.createElement('div');
  conversationHeader.className = 'nearby-section__header nearby-section__header--static';
  conversationHeader.append(textElement('span', 'nearby-section__title', '会话'));
  const createRoomButton = document.createElement('button');
  createRoomButton.type = 'button';
  createRoomButton.className = 'nearby-scan-button';
  createRoomButton.textContent = '+ 建群';
  createRoomButton.addEventListener('click', openCreateRoomPopover);
  conversationHeader.append(createRoomButton);
  const conversationList = document.createElement('div');
  conversationList.className = 'nearby-conv-list';
  conversationSection.append(conversationHeader, conversationList);

  element.append(identity, privacyLabel, discoverySection, conversationSection);

  identity.addEventListener('click', () => void openAgentSettings());
  identity.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      void openAgentSettings();
    }
  });
  privacyToggle.addEventListener('change', () => {
    actions.setDiscoverable(privacyToggle.checked);
  });

  let disposed = false;
  const overlays = new Set<HTMLElement>();

  function openOverlay(build: (popover: HTMLElement, close: () => void) => void): void {
    const overlay = document.createElement('div');
    overlay.className = 'nearby-popover-overlay';
    const popover = document.createElement('div');
    popover.className = 'nearby-popover';
    popover.setAttribute('role', 'dialog');
    const close = (): void => {
      overlay.remove();
      overlays.delete(overlay);
    };
    overlay.addEventListener('click', (event) => {
      if (event.target === overlay) close();
    });
    overlay.append(popover);
    overlays.add(overlay);
    element.append(overlay);
    build(popover, close);
  }

  function popoverFooter(close: () => void, onConfirm: () => void, confirmText: string): {
    footer: HTMLElement;
    confirm: HTMLButtonElement;
  } {
    const footer = document.createElement('div');
    footer.className = 'nearby-popover__footer';
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'nearby-secondary-action';
    cancel.textContent = '取消';
    cancel.addEventListener('click', close);
    const confirm = document.createElement('button');
    confirm.type = 'button';
    confirm.className = 'nearby-send-button';
    confirm.textContent = confirmText;
    confirm.addEventListener('click', onConfirm);
    footer.append(cancel, confirm);
    return { footer, confirm };
  }

  function connectedPeers(exclude: Set<string> = new Set()): NearbyPeer[] {
    return [...store.peers.values()].filter(
      (peer) => peer.connection === 'connected' && !exclude.has(peer.peer_id),
    );
  }

  function peerChecklist(peers: NearbyPeer[], selected: Set<string>, onChange: () => void): HTMLElement {
    const list = document.createElement('div');
    list.className = 'nearby-popover__list';
    if (peers.length === 0) {
      list.append(textElement('p', 'nearby-popover__empty', '没有已连接的同伴，请先在上方连接'));
      return list;
    }
    for (const peer of peers) {
      const row = document.createElement('label');
      row.className = 'nearby-popover__option';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.addEventListener('change', () => {
        if (box.checked) selected.add(peer.peer_id);
        else selected.delete(peer.peer_id);
        onChange();
      });
      const copy = document.createElement('span');
      copy.className = 'nearby-popover__option-label';
      copy.textContent = `${store.peerLabel(peer.peer_id)}（${store.peerAgentLabel(peer.peer_id)}）`;
      row.append(box, copy);
      list.append(row);
    }
    return list;
  }

  function agentModePicker(selected: { mode: NearbyAgentMode }): HTMLElement {
    const wrap = document.createElement('div');
    wrap.className = 'nearby-popover__modes';
    (Object.keys(AGENT_MODE_LABELS) as NearbyAgentMode[]).forEach((mode) => {
      const option = document.createElement('label');
      option.className = 'nearby-mode-option';
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'nearby-create-room-mode';
      radio.checked = selected.mode === mode;
      radio.addEventListener('change', () => {
        if (radio.checked) selected.mode = mode;
      });
      option.append(radio, textElement('span', 'nearby-mode-option__title', AGENT_MODE_LABELS[mode]));
      wrap.append(option);
    });
    return wrap;
  }

  function openCreateRoomPopover(): void {
    const peers = connectedPeers();
    const selected = new Set<string>();
    const modeState: { mode: NearbyAgentMode } = { mode: 'mention' };
    openOverlay((popover, close) => {
      popover.setAttribute('aria-label', '创建群聊');
      const nameInput = document.createElement('input');
      nameInput.className = 'nearby-popover__input';
      nameInput.maxLength = 120;
      nameInput.placeholder = '群聊名称，例如「XX 项目」';
      const { footer, confirm } = popoverFooter(close, () => {
        const name = nameInput.value.trim();
        if (!name || selected.size === 0) return;
        actions.createRoom(name, [...selected], modeState.mode);
        close();
      }, '创建');
      confirm.disabled = true;
      const syncConfirm = (): void => {
        confirm.disabled = !nameInput.value.trim() || selected.size === 0;
      };
      nameInput.addEventListener('input', syncConfirm);
      popover.append(
        textElement('h3', 'nearby-popover__title', '创建群聊'),
        nameInput,
        textElement('p', 'nearby-popover__label', '选择同伴（仅已连接）'),
        peerChecklist(peers, selected, syncConfirm),
        textElement('p', 'nearby-popover__label', 'Agent 触发模式'),
        agentModePicker(modeState),
        footer,
      );
    });
  }

  function openInvitePopover(peer: NearbyPeer): void {
    const ownedRooms = [...store.conversations.values()].filter(
      (conversation) => conversation.kind === 'room' && conversation.isOwner,
    );
    openOverlay((popover, close) => {
      popover.setAttribute('aria-label', '邀请进群');
      popover.append(textElement('h3', 'nearby-popover__title', `邀请 ${store.peerLabel(peer.peer_id)} 进群`));
      if (ownedRooms.length === 0) {
        popover.append(textElement('p', 'nearby-popover__empty', '你还没有创建的群聊，可先「+ 建群」'));
        const { footer } = popoverFooter(close, close, '知道了');
        popover.append(footer);
        return;
      }
      let pickedRoomId = ownedRooms.find((room) => !room.memberIds.includes(peer.peer_id))?.roomId
        ?? ownedRooms[0]?.roomId
        ?? '';
      const list = document.createElement('div');
      list.className = 'nearby-popover__list';
      for (const room of ownedRooms) {
        const row = document.createElement('label');
        row.className = 'nearby-popover__option';
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = 'nearby-invite-room';
        radio.checked = room.roomId === pickedRoomId;
        radio.addEventListener('change', () => {
          if (radio.checked) pickedRoomId = room.roomId;
        });
        const alreadyIn = room.memberIds.includes(peer.peer_id);
        if (alreadyIn) {
          radio.disabled = true;
          row.classList.add('nearby-popover__option--disabled');
        }
        row.append(radio, textElement(
          'span',
          'nearby-popover__option-label',
          alreadyIn ? `${room.title}（已在群内）` : room.title,
        ));
        list.append(row);
      }
      const { footer } = popoverFooter(close, () => {
        if (!pickedRoomId) return;
        actions.inviteToRoom(pickedRoomId, [peer.peer_id]);
        close();
      }, '邀请');
      popover.append(list, footer);
    });
  }

  async function openAgentSettings(): Promise<void> {
    const [settings, toolsets] = await Promise.all([
      actions.getAgentSettings(),
      actions.loadToolsets(),
    ]);
    if (disposed) return;
    store.applySettings(settings);
    const candidates = toolsets.length > 0 ? toolsets : FALLBACK_TOOLSETS;
    const chosen = new Set(settings.allowedToolsets);
    openOverlay((popover, close) => {
      popover.setAttribute('aria-label', '同伴 Agent 设置');
      const autoRow = document.createElement('label');
      autoRow.className = 'nearby-popover__option';
      const autoToggle = document.createElement('input');
      autoToggle.type = 'checkbox';
      autoToggle.checked = settings.autoReply;
      autoRow.append(autoToggle, textElement('span', 'nearby-popover__option-label', '允许 Agent 自动回复同伴'));
      autoToggle.addEventListener('change', () => {
        void actions.saveAgentSettings({ autoReply: autoToggle.checked });
      });

      const whitelist = document.createElement('div');
      whitelist.className = 'nearby-popover__list';
      for (const toolset of candidates) {
        const row = document.createElement('label');
        row.className = 'nearby-popover__option';
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.checked = chosen.has(toolset);
        box.addEventListener('change', () => {
          if (box.checked) chosen.add(toolset);
          else chosen.delete(toolset);
          void actions.saveAgentSettings({ allowedToolsets: [...chosen] });
        });
        row.append(box, textElement('span', 'nearby-popover__option-label', toolset));
        whitelist.append(row);
      }

      const { footer } = popoverFooter(close, close, '完成');
      popover.append(
        textElement('h3', 'nearby-popover__title', '同伴 Agent 设置'),
        autoRow,
        textElement('p', 'nearby-popover__label', '工具白名单（默认全部关闭）'),
        whitelist,
        textElement('p', 'nearby-popover__warning', '同伴场景下允许 Agent 使用这些能力，请仅为你信任的网络开启。敏感操作仍会经过 Ace 的安全审批。'),
        textElement('p', 'nearby-popover__note', '同时最多处理 8 个 Agent 回合，单条回复不超过 8000 字。'),
        footer,
      );
    });
  }

  function renderIdentity(): void {
    identityName.textContent = store.localName;
    identityAgent.textContent = store.localAgentName;
    identityAvatar.textContent = peerInitial(store.localName);
    identityStatus.textContent = store.discoverable ? '广播中' : '已关闭';
    identityStatus.dataset.on = store.discoverable ? 'true' : 'false';
    privacyToggle.checked = store.discoverable;
    privacyToggle.disabled = !store.localPeerId;
  }

  function peerCardAction(label: string, onClick: () => void, disabled = false): HTMLButtonElement {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'nearby-peer-card__action';
    button.textContent = label;
    button.disabled = disabled;
    button.addEventListener('click', (event) => {
      event.stopPropagation();
      onClick();
    });
    return button;
  }

  function renderPeers(): void {
    peerList.replaceChildren();
    scanButton.textContent = store.discovering ? '停止查找' : '开始查找';
    radar.dataset.scanning = store.discovering ? 'true' : 'false';
    const peers = [...store.peers.values()];
    if (peers.length === 0) {
      peerList.append(textElement(
        'p',
        'nearby-peer-list__empty',
        store.discovering
          ? '正在通过蓝牙寻找附近的 Ace…\n让身边的朋友打开 Ace 并开启「允许被发现」'
          : '查找已暂停',
      ));
      return;
    }
    for (const peer of peers) {
      const card = document.createElement('article');
      card.className = 'nearby-peer-card';
      card.dataset.connection = peer.connection;
      const avatar = textElement('span', 'nearby-peer-card__avatar', peerInitial(store.peerLabel(peer.peer_id)));
      const copy = document.createElement('span');
      copy.className = 'nearby-peer-card__copy';
      copy.append(
        textElement('strong', 'nearby-peer-card__name', store.peerLabel(peer.peer_id)),
        textElement('span', 'nearby-peer-card__agent', store.peerAgentLabel(peer.peer_id)),
        textElement('span', 'nearby-peer-card__connection', connectionLabel(peer.connection)),
      );
      const actionRow = document.createElement('span');
      actionRow.className = 'nearby-peer-card__actions';
      if (peer.connection === 'connected') {
        actionRow.append(
          peerCardAction('发消息', () => actions.selectConversation(dmConversationId(peer.peer_id))),
          peerCardAction('邀请进群', () => openInvitePopover(peer)),
          peerCardAction('断开', () => actions.disconnectPeer(peer.peer_id)),
        );
      } else if (peer.connection === 'connecting') {
        actionRow.append(peerCardAction('连接中', () => undefined, true));
      } else if (peer.connection === 'unavailable') {
        actionRow.append(peerCardAction('已离开', () => undefined, true));
      } else {
        actionRow.append(peerCardAction('连接', () => actions.connectPeer(peer.peer_id)));
      }
      card.append(avatar, copy, actionRow);
      peerList.append(card);
    }
  }

  function conversationItem(conversation: NearbyConversation): HTMLElement {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = [
      'nearby-conv-item',
      conversation.id === store.activeConversationId ? 'nearby-conv-item--active' : '',
    ].filter(Boolean).join(' ');
    const avatar = textElement(
      'span',
      'nearby-conv-item__avatar',
      conversation.kind === 'room' ? '群' : peerInitial(conversation.title),
    );
    const copy = document.createElement('span');
    copy.className = 'nearby-conv-item__copy';
    copy.append(
      textElement('strong', 'nearby-conv-item__name', conversation.title),
      textElement('span', 'nearby-conv-item__preview', conversation.lastMessageText || '暂无消息'),
    );
    const meta = document.createElement('span');
    meta.className = 'nearby-conv-item__meta';
    meta.append(textElement('span', 'nearby-conv-item__time', conversationTime(conversation.lastMessageAt)));
    if (conversation.unread > 0) {
      meta.append(textElement(
        'span',
        'nearby-conv-item__badge',
        conversation.unread > 99 ? '99+' : String(conversation.unread),
      ));
    }
    item.append(avatar, copy, meta);
    item.addEventListener('click', () => actions.selectConversation(conversation.id));
    return item;
  }

  function renderConversations(): void {
    conversationList.replaceChildren();
    const sorted = [...store.conversations.values()].sort((a, b) => b.lastMessageAt - a.lastMessageAt);
    const rooms = sorted.filter((conversation) => conversation.kind === 'room');
    const dms = sorted.filter((conversation) => conversation.kind === 'dm');
    if (rooms.length === 0 && dms.length === 0) {
      conversationList.append(textElement('p', 'nearby-conv-list__empty', '还没有会话。连接同伴或创建一个群开始。'));
      return;
    }
    if (rooms.length > 0) {
      conversationList.append(textElement('p', 'nearby-conv-list__group', '群聊'));
      for (const room of rooms) conversationList.append(conversationItem(room));
    }
    if (dms.length > 0) {
      conversationList.append(textElement('p', 'nearby-conv-list__group', '直聊'));
      for (const dm of dms) conversationList.append(conversationItem(dm));
    }
  }

  return {
    element,
    render(): void {
      if (disposed) return;
      renderIdentity();
      renderPeers();
      renderConversations();
    },
    dispose(): void {
      disposed = true;
      for (const overlay of overlays) overlay.remove();
      overlays.clear();
    },
  };
}
