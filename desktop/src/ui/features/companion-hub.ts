/**
 * 同伴管理中枢。
 *
 * 这个页面只负责发现、关系、群和公开名片；消息流始终在主对话中展示。
 */

import { createIcon } from '../components/icon';
import type { CompanionAgentCandidate } from '../backend-client';
import type { NearbyAgentMode, NearbyConversation, NearbyPeer, NearbyStore } from './nearby-store';

export interface CompanionHubActions {
  connectPeer(peerId: string): void;
  disconnectPeer(peerId: string): void;
  setDiscoverable(enabled: boolean): void;
  openConversation(conversationId: string): void;
  createRoom(name: string, memberIds: string[], agentMode: NearbyAgentMode): void;
  loadAgentCandidates(): Promise<CompanionAgentCandidate[]>;
  savePublishedAgents(sourceRefs: string[]): Promise<CompanionAgentCandidate[]>;
  showStatus(text: string, tone?: 'normal' | 'error'): void;
}

export interface CompanionHub {
  readonly element: HTMLElement;
  render(): void;
  showDiscovery(): void;
  showProfile(): void;
  showConversation(conversationId: string): void;
  dispose(): void;
}

type HubView = 'discovery' | 'profile' | 'conversation';

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

function avatar(label: string, options: { agent?: boolean; group?: boolean; large?: boolean } = {}): HTMLSpanElement {
  const element = textElement('span', 'companion-avatar', label.trim().slice(0, 1).toUpperCase() || '同');
  if (options.agent) element.classList.add('is-agent');
  if (options.group) element.classList.add('is-group');
  if (options.large) element.classList.add('is-large');
  return element;
}

function iconButton(label: string, icon: string, onClick: () => void): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'companion-icon-button';
  button.setAttribute('aria-label', label);
  button.title = label;
  button.append(createIcon(icon, { size: 18 }));
  button.addEventListener('click', onClick);
  return button;
}

function actionButton(label: string, primary = false): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = primary ? 'companion-button is-primary' : 'companion-button';
  button.textContent = label;
  return button;
}

function peerAgent(peer: NearbyPeer): { name: string; description: string } | null {
  const published = peer.published_agents[0];
  if (published) {
    return {
      name: published.display_name,
      description: published.description || '可以在主人所在的群聊中参与协作',
    };
  }
  if (!peer.agent_name.trim()) return null;
  return { name: peer.agent_name.trim(), description: '可以在主人所在的群聊中参与协作' };
}

function connectionLabel(peer: NearbyPeer): string {
  if (peer.connection === 'connected') return '已连接';
  if (peer.connection === 'connecting') return '正在连接';
  if (peer.connection === 'disconnected') return '暂时离线';
  if (peer.connection === 'unavailable') return '信号较弱';
  return '就在附近';
}

export function createCompanionHub(options: {
  store: NearbyStore;
  actions: CompanionHubActions;
}): CompanionHub {
  const { store, actions } = options;
  const element = document.createElement('div');
  element.className = 'companion-hub';
  const rail = document.createElement('aside');
  rail.className = 'companion-rail';
  const main = document.createElement('main');
  main.className = 'companion-main';
  element.append(rail, main);

  let view: HubView = 'discovery';
  let selectedConversationId = '';
  let focusedPeerId = '';
  let query = '';
  let publicationCandidates: CompanionAgentCandidate[] | null = null;
  let publicationLoading = false;
  let groupSheet: HTMLElement | null = null;

  const peers = (): NearbyPeer[] => [...store.peers.values()]
    .filter((peer) => peer.connection !== 'unavailable')
    .sort((left, right) => {
      const rank = (peer: NearbyPeer) => peer.connection === 'connected' ? 0 : 1;
      return rank(left) - rank(right) || left.display_name.localeCompare(right.display_name);
    });

  const conversations = (): NearbyConversation[] => [...store.conversations.values()]
    .sort((left, right) => right.lastMessageAt - left.lastMessageAt || left.title.localeCompare(right.title));

  const selectedConversation = (): NearbyConversation | null => (
    selectedConversationId ? store.conversations.get(selectedConversationId) ?? null : null
  );

  function setView(next: HubView, conversationId = ''): void {
    view = next;
    selectedConversationId = conversationId;
    if (conversationId) store.setActiveConversation(conversationId);
    render();
  }

  function showGroupSheet(): void {
    groupSheet?.remove();
    const connected = peers().filter((peer) => peer.connection === 'connected');
    const overlay = document.createElement('div');
    overlay.className = 'companion-sheet-overlay';
    overlay.setAttribute('role', 'presentation');
    const sheet = document.createElement('section');
    sheet.className = 'companion-sheet';
    sheet.setAttribute('role', 'dialog');
    sheet.setAttribute('aria-modal', 'true');
    sheet.setAttribute('aria-labelledby', 'companion-create-group-title');
    const head = document.createElement('header');
    head.className = 'companion-sheet__header';
    const heading = document.createElement('div');
    const title = textElement('h2', '', '创建群聊');
    title.id = 'companion-create-group-title';
    heading.append(title, textElement('p', '', '先选择人和群名，Agent 不会被自动带入。'));
    head.append(heading, iconButton('关闭', 'icon-close', () => overlay.remove()));

    const name = document.createElement('input');
    name.className = 'companion-sheet__name';
    name.placeholder = '群聊名称';
    name.maxLength = 60;
    const memberList = document.createElement('div');
    memberList.className = 'companion-sheet__members';
    const selected = new Set<string>();
    if (connected.length === 0) {
      memberList.append(textElement('p', 'companion-empty-inline', '先连接一位同伴，再创建群聊。'));
    }
    for (const peer of connected) {
      const label = document.createElement('label');
      label.className = 'companion-member-option';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) selected.add(peer.peer_id);
        else selected.delete(peer.peer_id);
      });
      const copy = document.createElement('span');
      copy.append(textElement('strong', '', store.peerLabel(peer.peer_id)), textElement('small', '', '已连接'));
      label.append(checkbox, avatar(store.peerLabel(peer.peer_id)), copy);
      memberList.append(label);
    }
    const footer = document.createElement('footer');
    footer.className = 'companion-sheet__footer';
    const cancel = actionButton('取消');
    cancel.addEventListener('click', () => overlay.remove());
    const create = actionButton('创建', true);
    create.disabled = connected.length === 0;
    create.addEventListener('click', () => {
      const roomName = name.value.trim();
      if (!roomName) {
        name.focus();
        return;
      }
      if (selected.size === 0) {
        actions.showStatus('请至少选择一位已连接的同伴', 'error');
        return;
      }
      actions.createRoom(roomName, [...selected], 'mention');
      overlay.remove();
    });
    footer.append(cancel, create);
    sheet.append(head, name, memberList, footer);
    overlay.append(sheet);
    overlay.addEventListener('click', (event) => {
      if (event.target === overlay) overlay.remove();
    });
    element.append(overlay);
    groupSheet = overlay;
    requestAnimationFrame(() => name.focus());
  }

  function renderRail(): void {
    rail.replaceChildren();
    const header = document.createElement('div');
    header.className = 'companion-rail__header';
    header.append(
      textElement('h2', '', '同伴'),
      (() => {
        const actionsWrap = document.createElement('div');
        actionsWrap.className = 'companion-rail__actions';
        actionsWrap.append(
          iconButton('创建群聊', 'icon-plus', showGroupSheet),
          iconButton('我的名片', 'icon-more', () => setView('profile')),
        );
        return actionsWrap;
      })(),
    );
    const search = document.createElement('label');
    search.className = 'companion-search';
    const searchInput = document.createElement('input');
    searchInput.type = 'search';
    searchInput.placeholder = '搜索人、Agent 或群';
    searchInput.value = query;
    searchInput.addEventListener('input', () => {
      query = searchInput.value;
      renderRail();
    });
    search.append(createIcon('icon-search', { size: 16 }), searchInput);

    const nearby = document.createElement('section');
    nearby.className = 'companion-nearby-strip';
    const nearbyHeader = document.createElement('div');
    nearbyHeader.className = 'companion-section-label';
    nearbyHeader.append(textElement('span', '', `就在附近 · ${peers().length}`));
    const faces = document.createElement('div');
    faces.className = 'companion-nearby-faces';
    for (const peer of peers().slice(0, 5)) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'companion-nearby-face';
      button.title = store.peerLabel(peer.peer_id);
      button.append(avatar(store.peerLabel(peer.peer_id)), textElement('small', '', store.peerLabel(peer.peer_id)));
      button.addEventListener('click', () => {
        focusedPeerId = peer.peer_id;
        setView('discovery');
      });
      faces.append(button);
    }
    if (peers().length === 0) faces.append(textElement('span', 'companion-empty-inline', '正在寻找…'));
    nearby.append(nearbyHeader, faces);

    const list = document.createElement('div');
    list.className = 'companion-rail-list';
    list.append(textElement('p', 'companion-section-label', '管理'));
    const managementRows: Array<[string, string, string, () => void, boolean]> = [
      ['遇', '发现', `${peers().length} 位就在附近`, () => setView('discovery'), view === 'discovery'],
      ['我', '我的名片', store.discoverable ? '附近可见' : '已隐藏', () => setView('profile'), view === 'profile'],
    ];
    for (const [symbol, title, subtitle, onClick, active] of managementRows) {
      list.append(railRow({ symbol, title, subtitle, onClick, active }));
    }

    const normalizedQuery = query.trim().toLocaleLowerCase();
    const filtered = conversations().filter((conversation) => {
      if (!normalizedQuery) return true;
      const agentName = conversation.kind === 'dm' ? store.peerAgentLabel(conversation.peerId) : '';
      return `${conversation.title} ${agentName}`.toLocaleLowerCase().includes(normalizedQuery);
    });
    const direct = filtered.filter((conversation) => conversation.kind === 'dm');
    const rooms = filtered.filter((conversation) => conversation.kind === 'room');
    if (direct.length > 0) list.append(textElement('p', 'companion-section-label', `同伴 · ${direct.length}`));
    for (const conversation of direct) {
      const peer = store.peers.get(conversation.peerId);
      list.append(railRow({
        symbol: conversation.title,
        title: conversation.title,
        subtitle: `${peer ? connectionLabel(peer) : '暂时离线'} · 带着 ${store.peerAgentLabel(conversation.peerId)}`,
        onClick: () => setView('conversation', conversation.id),
        active: view === 'conversation' && selectedConversationId === conversation.id,
        unread: conversation.unread,
      }));
    }
    if (rooms.length > 0) list.append(textElement('p', 'companion-section-label', `群聊 · ${rooms.length}`));
    for (const conversation of rooms) {
      list.append(railRow({
        symbol: conversation.title,
        title: conversation.title,
        subtitle: `${conversation.memberIds.length} 人 · Agent 席位按主人授权`,
        onClick: () => setView('conversation', conversation.id),
        active: view === 'conversation' && selectedConversationId === conversation.id,
        group: true,
        unread: conversation.unread,
      }));
    }
    rail.append(header, search, nearby, list);
  }

  function railRow(options: {
    symbol: string;
    title: string;
    subtitle: string;
    onClick: () => void;
    active?: boolean;
    group?: boolean;
    unread?: number;
  }): HTMLButtonElement {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'companion-rail-row';
    row.classList.toggle('is-active', Boolean(options.active));
    const copy = document.createElement('span');
    copy.className = 'companion-rail-row__copy';
    copy.append(textElement('strong', '', options.title), textElement('small', '', options.subtitle));
    row.append(avatar(options.symbol, { group: Boolean(options.group) }), copy);
    if (options.unread) row.append(textElement('span', 'companion-unread', String(options.unread)));
    row.addEventListener('click', options.onClick);
    return row;
  }

  function renderMainHeader(
    title: string,
    subtitle: string,
    action?: { label: string; run: () => void; disabled?: boolean; disabledHint?: string },
  ): HTMLElement {
    const header = document.createElement('header');
    header.className = 'companion-main__header';
    const copy = document.createElement('div');
    copy.className = 'companion-main__title';
    copy.append(textElement('strong', '', title), textElement('span', '', subtitle));
    header.append(copy);
    if (action) {
      const button = actionButton(action.label, true);
      button.disabled = Boolean(action.disabled);
      if (action.disabledHint) button.title = action.disabledHint;
      button.addEventListener('click', action.run);
      header.append(button);
    }
    return header;
  }

  function renderDiscovery(): void {
    const allPeers = peers();
    const focused = allPeers.find((peer) => peer.peer_id === focusedPeerId) ?? allPeers[0];
    if (focused) focusedPeerId = focused.peer_id;
    main.append(renderMainHeader('遇见附近的 Ace', '先认识人，再决定要不要一起做事'));
    const canvas = document.createElement('div');
    canvas.className = 'companion-canvas companion-discovery';
    const heading = document.createElement('div');
    heading.className = 'companion-canvas__heading';
    const copy = document.createElement('div');
    copy.append(
      textElement('h1', '', `现在，附近有 ${allPeers.length} 位同伴`),
      textElement('p', '', '只展示大致距离，不公开设备名和硬件信息。'),
    );
    const visibility = textElement('span', 'companion-visibility', store.discoverable ? '附近可见' : '已隐藏');
    heading.append(copy, visibility);
    canvas.append(heading);
    if (!focused) {
      const empty = document.createElement('section');
      empty.className = 'companion-empty-state';
      empty.append(
        avatar('遇', { large: true }),
        textElement('h2', '', '正在寻找附近的 Ace'),
        textElement('p', '', '让身边的朋友打开 Ace 并允许被发现，出现后会自动显示在这里。'),
      );
      canvas.append(empty);
      main.append(canvas);
      return;
    }
    const card = document.createElement('article');
    card.className = 'companion-meet-card';
    const person = document.createElement('section');
    person.className = 'companion-meet-card__person';
    const visual = document.createElement('div');
    visual.className = 'companion-meet-card__visual';
    visual.append(avatar(store.peerLabel(focused.peer_id), { large: true }));
    const agent = peerAgent(focused);
    if (agent) visual.append(avatar(agent.name, { agent: true }));
    person.append(
      visual,
      textElement('h2', '', store.peerLabel(focused.peer_id)),
      textElement('p', 'companion-meet-card__status', `${connectionLabel(focused)} · 通过 Ace 同伴被发现`),
    );
    const actionsWrap = document.createElement('div');
    actionsWrap.className = 'companion-meet-card__actions';
    const hello = actionButton(focused.connection === 'connected' ? '查看同伴' : '打个招呼', true);
    hello.addEventListener('click', () => {
      if (focused.connection === 'connected') {
        const conversation = store.conversations.get(`dm:${focused.peer_id}`);
        if (conversation) setView('conversation', conversation.id);
      } else {
        actions.connectPeer(focused.peer_id);
      }
    });
    const skip = actionButton('暂时略过');
    skip.addEventListener('click', () => {
      const index = allPeers.findIndex((peer) => peer.peer_id === focused.peer_id);
      focusedPeerId = allPeers[(index + 1) % allPeers.length]?.peer_id ?? '';
      render();
    });
    actionsWrap.append(hello, skip);
    person.append(actionsWrap);
    const side = document.createElement('aside');
    side.className = 'companion-meet-card__agent';
    if (agent) {
      side.append(
        textElement('small', '', '随行 Agent · 仅作预览'),
        textElement('h3', '', agent.name),
        textElement('p', '', agent.description),
        ruleList([
          ['参与方式', `只能加入 ${store.peerLabel(focused.peer_id)} 所在的群聊`],
          ['安全边界', '不可私聊，不随同伴关系自动授权'],
        ]),
      );
    } else {
      side.append(textElement('small', '', '随行 Agent'), textElement('h3', '', '未公开 Agent'), textElement('p', '', '对方当前只公开了人物名片。'));
    }
    card.append(person, side);
    canvas.append(card);
    main.append(canvas);
  }

  function ruleList(items: Array<[string, string]>): HTMLElement {
    const list = document.createElement('div');
    list.className = 'companion-rule-list';
    for (const [name, value] of items) {
      const row = document.createElement('div');
      row.append(textElement('strong', '', name), textElement('span', '', value));
      list.append(row);
    }
    return list;
  }

  function renderConversation(): void {
    const conversation = selectedConversation();
    if (!conversation) {
      view = 'discovery';
      renderDiscovery();
      return;
    }
    const isRoom = conversation.kind === 'room';
    const online = store.isConversationOnline(conversation);
    main.append(renderMainHeader(
      isRoom ? conversation.title : '同伴资料',
      isRoom ? '管理成员、Agent 席位和群权限' : '管理关系与可一起加入的群',
      {
        label: online ? (isRoom ? '进入群聊' : '私聊本人') : (isRoom ? '暂无成员在线' : '本人离线'),
        run: () => actions.openConversation(conversation.id),
        disabled: !online,
        disabledHint: online ? '' : (isRoom ? '群内至少需要一位远端成员在线' : '重新连接后才能发消息'),
      },
    ));
    const canvas = document.createElement('div');
    canvas.className = 'companion-canvas companion-management';
    const heading = document.createElement('div');
    heading.className = 'companion-canvas__heading';
    const copy = document.createElement('div');
    const peer = isRoom ? null : store.peers.get(conversation.peerId);
    copy.append(
      textElement('h1', '', isRoom ? `${conversation.memberIds.length} 位成员` : conversation.title),
      textElement('p', '', isRoom
        ? 'Agent 只在主人仍是本群成员时可用。'
        : `${peer ? connectionLabel(peer) : '暂时离线'} · 消息只发送给本人，不会调用随行 Agent。`),
    );
    heading.append(copy, textElement('span', 'companion-source-chip', isRoom ? '同伴群聊' : 'Nearby'));
    const grid = document.createElement('div');
    grid.className = 'companion-management-grid';
    if (isRoom) renderRoomPanels(grid, conversation);
    else renderPeerPanels(grid, conversation, peer ?? null);
    canvas.append(heading, grid);
    main.append(canvas);
  }

  function panel(title: string, subtitle: string): HTMLElement {
    const section = document.createElement('section');
    section.className = 'companion-panel';
    section.append(textElement('h2', '', title), textElement('p', 'companion-panel__lead', subtitle));
    return section;
  }

  function profileRow(name: string, subtitle: string, options: { agent?: boolean; trailing?: string } = {}): HTMLElement {
    const row = document.createElement('div');
    row.className = 'companion-profile-row';
    const copy = document.createElement('div');
    copy.className = 'companion-profile-row__copy';
    copy.append(textElement('strong', '', name), textElement('span', '', subtitle));
    row.append(avatar(name, { agent: Boolean(options.agent) }), copy);
    if (options.trailing) row.append(textElement('span', 'companion-tag', options.trailing));
    return row;
  }

  function renderPeerPanels(grid: HTMLElement, conversation: NearbyConversation, peer: NearbyPeer | null): void {
    const person = panel('人物名片', '人与人的关系和私聊入口都在这里管理。');
    person.append(
      profileRow(conversation.title, `${peer ? connectionLabel(peer) : '暂时离线'} · 可邀请进群`, { trailing: '同伴' }),
      ruleList([
        ['关系权限', '可私聊、可查看在线状态、可互相拉群'],
        ['通信入口', peer?.connection === 'connected'
          ? `点击“私聊本人”后进入主对话；消息发送给 ${conversation.title} 本人`
          : '对方本人重新上线后才能进入主对话'],
      ]),
    );
    const agentPanel = panel('随行 Agent', 'Agent 是对方的群内能力，不是联系人。');
    const agent = peer ? peerAgent(peer) : null;
    if (agent) {
      agentPanel.append(
        profileRow(agent.name, `归属于 ${conversation.title}`, { agent: true, trailing: '邀请进群' }),
        ruleList([
          ['参与方式', `只有 ${conversation.title} 仍在群里时才可加入并被 @`],
          ['私聊', '永久关闭，不能被任何人开启'],
        ]),
      );
    } else {
      agentPanel.append(textElement('p', 'companion-empty-inline', '这位同伴暂未公开 Agent。'));
    }
    grid.append(person, agentPanel);
  }

  function renderRoomPanels(grid: HTMLElement, conversation: NearbyConversation): void {
    const people = panel('人类成员', '先管理人，再管理各自的 Agent。');
    const memberIds = conversation.memberIds.includes(store.localPeerId)
      ? conversation.memberIds
      : [store.localPeerId, ...conversation.memberIds];
    for (const memberId of memberIds) {
      const local = memberId === store.localPeerId;
      const memberPeer = local ? null : store.peers.get(memberId);
      people.append(profileRow(
        local ? store.localName : store.peerLabel(memberId),
        local ? (conversation.isOwner ? '群主 · 本机' : '成员 · 本机') : connectionLabel(memberPeer ?? {
          peer_id: memberId, display_name: '', agent_name: '', capabilities: [], published_agents: [], connection: 'disconnected',
        }),
        { trailing: local ? `带着 ${store.localAgentName}` : `带着 ${store.peerAgentLabel(memberId)}` },
      ));
    }
    const seats = panel('Agent 席位', '每个 Agent 都对应一位仍在群中的主人。');
    seats.append(profileRow(store.localAgentName, `主人：${store.localName} · 可被 @`, { agent: true, trailing: '本机' }));
    for (const memberId of conversation.memberIds.filter((id) => id !== store.localPeerId)) {
      const memberPeer = store.peers.get(memberId);
      const published = memberPeer ? peerAgent(memberPeer) : null;
      if (published) seats.append(profileRow(published.name, `主人：${store.peerLabel(memberId)} · 仅展示最终输出`, { agent: true, trailing: '远端' }));
    }
    seats.append(ruleList([['自动约束', '主人离群后，所属 Agent 自动离群；历史消息保留。']]));
    grid.append(people, seats);
  }

  function loadPublications(): void {
    if (publicationCandidates || publicationLoading) return;
    publicationLoading = true;
    void actions.loadAgentCandidates().then((candidates) => {
      publicationCandidates = candidates;
      publicationLoading = false;
      if (view === 'profile') render();
    }).catch((error: unknown) => {
      publicationLoading = false;
      actions.showStatus(error instanceof Error ? error.message : String(error), 'error');
    });
  }

  function renderProfile(): void {
    loadPublications();
    main.append(renderMainHeader('我的 Agent 名片', '决定附近的人能看到哪些 Agent'));
    const canvas = document.createElement('div');
    canvas.className = 'companion-canvas companion-management';
    const heading = document.createElement('div');
    heading.className = 'companion-canvas__heading';
    const copy = document.createElement('div');
    copy.append(textElement('h1', '', store.localName), textElement('p', '', '我的公开资料与群内协作能力'));
    const toggleLabel = document.createElement('label');
    toggleLabel.className = 'companion-discoverable-toggle';
    const toggle = document.createElement('input');
    toggle.type = 'checkbox';
    toggle.checked = store.discoverable;
    toggle.addEventListener('change', () => actions.setDiscoverable(toggle.checked));
    toggleLabel.append(toggle, textElement('span', '', '允许被发现'));
    heading.append(copy, toggleLabel);
    const grid = document.createElement('div');
    grid.className = 'companion-management-grid';
    const profile = panel('我的资料', '对方只能看到你主动公开的信息。');
    profile.append(profileRow(store.localName, store.discoverable ? '附近可见' : '已对附近隐藏', { trailing: '本人' }));
    const agents = panel('公开哪些 Agent', '默认只公开 Crew；Agent 仅能加入你所在的群聊，不能私聊。');
    const form = document.createElement('form');
    form.className = 'companion-publications';
    if (publicationLoading || publicationCandidates === null) {
      form.append(textElement('p', 'companion-empty-inline', '正在读取 Agent…'));
    } else if (publicationCandidates.length === 0) {
      form.append(textElement('p', 'companion-empty-inline', '暂时没有可公开的 Agent。'));
    } else {
      for (const candidate of publicationCandidates) {
        const label = document.createElement('label');
        label.className = 'companion-publication-option';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.name = 'published-agent';
        checkbox.value = candidate.source_ref;
        checkbox.checked = candidate.published;
        const copyBox = document.createElement('span');
        copyBox.append(
          textElement('strong', '', candidate.display_name),
          textElement('small', '', candidate.source_kind === 'external' ? '外援 Agent · 群内可用' : '本地 Agent · 群内可用'),
        );
        label.append(checkbox, avatar(candidate.display_name, { agent: true }), copyBox);
        form.append(label);
      }
      const save = actionButton('保存公开范围', true);
      save.type = 'submit';
      form.append(save);
      form.addEventListener('submit', (event) => {
        event.preventDefault();
        save.disabled = true;
        const sourceRefs = [...form.querySelectorAll<HTMLInputElement>('input[name="published-agent"]:checked')]
          .map((input) => input.value);
        void actions.savePublishedAgents(sourceRefs).then((candidates) => {
          publicationCandidates = candidates;
          actions.showStatus('Agent 公开范围已保存');
          render();
        }).catch((error: unknown) => {
          save.disabled = false;
          actions.showStatus(error instanceof Error ? error.message : String(error), 'error');
        });
      });
    }
    agents.append(form);
    grid.append(profile, agents);
    canvas.append(heading, grid);
    main.append(canvas);
  }

  function render(): void {
    renderRail();
    main.replaceChildren();
    if (view === 'profile') renderProfile();
    else if (view === 'conversation') renderConversation();
    else renderDiscovery();
  }

  return {
    element,
    render,
    showDiscovery: () => setView('discovery'),
    showProfile: () => setView('profile'),
    showConversation: (conversationId) => setView('conversation', conversationId),
    dispose(): void {
      groupSheet?.remove();
      element.replaceChildren();
    },
  };
}
