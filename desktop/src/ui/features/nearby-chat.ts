/**
 * 同伴页聊天窗：直聊/群聊统一的消息流 + 输入区，以及群聊右侧的成员/设置面板。
 *
 * 只做渲染与用户输入编排；状态读取自 NearbyStore，指令经由 NearbyActions
 * 下发到主进程。群成员面板（右栏 260px）由本模块持有，仅群聊会话可见。
 */

import {
  AGENT_MODE_LABELS,
  type NearbyAgentMode,
  type NearbyConversation,
  type NearbyFileCard,
  type NearbyStore,
} from './nearby-store';
import type { NearbyActions, NearbyMention } from './nearby-page';

const MAX_MESSAGE_CHARS = 8_000;
const LONG_TEXT_CLAMP = 600;
const MENTION_PATTERN = /@([^\s@]{0,24})$/;

interface MentionCandidate {
  peerId: string;
  kind: 'person' | 'agent';
  label: string;
}

export interface NearbyChatPane {
  element: HTMLElement;
  panelElement: HTMLElement;
  render(): void;
  dispose(): void;
  focusComposer(): void;
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

function messageTime(timestamp: number): string {
  if (!timestamp) return '';
  return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(new Date(timestamp));
}

function formatFileSize(size: number): string {
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${size} B`;
}

export function createNearbyChat(deps: {
  store: NearbyStore;
  actions: NearbyActions;
  workspace: HTMLElement;
}): NearbyChatPane {
  const { store, actions, workspace } = deps;

  const element = document.createElement('section');
  element.className = 'nearby-conversation';

  const emptyState = document.createElement('div');
  emptyState.className = 'nearby-empty-state';
  emptyState.append(
    textElement('span', 'nearby-empty-state__symbol', '⌁'),
    textElement('h2', 'nearby-empty-state__title', '选择一个会话开始协同'),
    textElement('p', 'nearby-empty-state__copy', '连接附近的 Ace 后会生成直聊会话，也可以把多个同伴拉进一个群。'),
  );

  const chat = document.createElement('div');
  chat.className = 'nearby-chat';
  chat.hidden = true;

  const chatHeader = document.createElement('header');
  chatHeader.className = 'nearby-chat__header';
  const chatIdentity = document.createElement('div');
  chatIdentity.className = 'nearby-chat__identity';
  const chatAvatar = textElement('span', 'nearby-chat__avatar', '?');
  const chatCopy = document.createElement('div');
  const chatName = textElement('h2', 'nearby-chat__name', '');
  const chatState = textElement('p', 'nearby-chat__state', '');
  chatCopy.append(chatName, chatState);
  chatIdentity.append(chatAvatar, chatCopy);
  const headerActions = document.createElement('div');
  headerActions.className = 'nearby-chat__header-actions';
  const membersButton = document.createElement('button');
  membersButton.type = 'button';
  membersButton.className = 'nearby-secondary-action';
  membersButton.textContent = '成员';
  const disconnectButton = document.createElement('button');
  disconnectButton.type = 'button';
  disconnectButton.className = 'nearby-secondary-action';
  disconnectButton.textContent = '断开';
  headerActions.append(membersButton, disconnectButton);
  chatHeader.append(chatIdentity, headerActions);

  const messageList = document.createElement('div');
  messageList.className = 'nearby-chat__messages';
  messageList.setAttribute('aria-live', 'polite');

  const composerHint = textElement('p', 'nearby-composer__hint', '');
  composerHint.hidden = true;
  const composer = document.createElement('form');
  composer.className = 'nearby-composer';
  const attachButton = document.createElement('button');
  attachButton.type = 'button';
  attachButton.className = 'nearby-composer__attach';
  attachButton.textContent = '📎';
  attachButton.title = '发送文件（不超过 4 MiB）';
  const inputWrap = document.createElement('div');
  inputWrap.className = 'nearby-composer__field';
  const messageInput = document.createElement('textarea');
  messageInput.className = 'nearby-composer__input';
  messageInput.rows = 2;
  messageInput.maxLength = MAX_MESSAGE_CHARS;
  const mentionPopup = document.createElement('div');
  mentionPopup.className = 'nearby-mention';
  mentionPopup.hidden = true;
  inputWrap.append(mentionPopup, messageInput);
  const sendButton = document.createElement('button');
  sendButton.type = 'submit';
  sendButton.className = 'nearby-send-button';
  sendButton.textContent = '发送';
  sendButton.disabled = true;
  composer.append(attachButton, inputWrap, sendButton);
  chat.append(chatHeader, messageList, composerHint, composer);
  element.append(emptyState, chat);

  const panelElement = document.createElement('aside');
  panelElement.className = 'nearby-panel';
  panelElement.hidden = true;

  // 会话内的本地 UI 状态
  let panelOpen = true;
  let renderedConversationId: string | null = null;
  let renderedMessageCount = -1;
  const expandedMessages = new Set<string>();
  const drafts = new Map<string, string>();
  let mentionMap = new Map<string, MentionCandidate>();
  let mentionCandidates: MentionCandidate[] = [];
  let mentionIndex = 0;
  let mentionAnchor = -1;
  let hintTimer: ReturnType<typeof setTimeout> | null = null;
  let confirmLeaveArmed = false;
  let confirmLeaveTimer: ReturnType<typeof setTimeout> | null = null;
  let disposed = false;

  const activeConversation = (): NearbyConversation | null => {
    const id = store.activeConversationId;
    return id ? store.conversations.get(id) ?? null : null;
  };

  const showHint = (text: string, sticky = false): void => {
    composerHint.textContent = text;
    composerHint.hidden = !text;
    if (hintTimer) clearTimeout(hintTimer);
    hintTimer = null;
    if (text && !sticky) {
      hintTimer = setTimeout(() => {
        composerHint.hidden = true;
        hintTimer = null;
      }, 4_000);
    }
  };

  const closeMentionPopup = (): void => {
    mentionPopup.hidden = true;
    mentionAnchor = -1;
  };

  const mentionCandidatesFor = (conversation: NearbyConversation): MentionCandidate[] => {
    if (conversation.kind === 'dm') {
      const peerId = conversation.peerId;
      return [
        { peerId, kind: 'person', label: store.peerLabel(peerId) },
        { peerId, kind: 'agent', label: store.peerAgentLabel(peerId) },
      ];
    }
    const candidates: MentionCandidate[] = [];
    for (const memberId of conversation.memberIds) {
      if (memberId === store.localPeerId) continue;
      candidates.push({ peerId: memberId, kind: 'person', label: store.peerLabel(memberId) });
    }
    for (const memberId of conversation.memberIds) {
      if (memberId === store.localPeerId) continue;
      candidates.push({ peerId: memberId, kind: 'agent', label: store.peerAgentLabel(memberId) });
    }
    return candidates;
  };

  const renderMentionPopup = (): void => {
    mentionPopup.replaceChildren();
    const conversation = activeConversation();
    if (!conversation) return;
    const quiet = conversation.kind === 'room' && conversation.agentMode === 'quiet';
    const groups: Array<{ title: string; kind: MentionCandidate['kind'] }> = [
      { title: '同伴', kind: 'person' },
      { title: 'Agent', kind: 'agent' },
    ];
    let flatIndex = 0;
    for (const group of groups) {
      const items = mentionCandidates.filter((candidate) => candidate.kind === group.kind);
      if (items.length === 0) continue;
      mentionPopup.append(textElement('p', 'nearby-mention__group', group.title));
      for (const candidate of items) {
        const disabled = quiet && candidate.kind === 'agent';
        const item = document.createElement('button');
        item.type = 'button';
        item.className = [
          'nearby-mention__item',
          flatIndex === mentionIndex ? 'nearby-mention__item--active' : '',
          disabled ? 'nearby-mention__item--disabled' : '',
        ].filter(Boolean).join(' ');
        item.append(
          textElement('span', 'nearby-mention__label', `@${candidate.label}`),
          textElement('span', 'nearby-mention__tag', candidate.kind === 'agent' ? (disabled ? 'Agent · 安静模式' : 'Agent') : '人'),
        );
        const index = flatIndex;
        item.addEventListener('click', () => pickMention(index));
        mentionPopup.append(item);
        flatIndex += 1;
      }
    }
  };

  const pickMention = (index: number): void => {
    const candidate = mentionCandidates[index];
    const conversation = activeConversation();
    if (!candidate || !conversation || mentionAnchor < 0) return;
    if (candidate.kind === 'agent' && conversation.kind === 'room' && conversation.agentMode === 'quiet') {
      showHint('本群已开启安静模式，Agent 不会响应');
      closeMentionPopup();
      return;
    }
    const caret = messageInput.selectionStart ?? messageInput.value.length;
    const before = messageInput.value.slice(0, mentionAnchor);
    const after = messageInput.value.slice(caret);
    const inserted = `@${candidate.label} `;
    messageInput.value = `${before}${inserted}${after}`;
    const nextCaret = before.length + inserted.length;
    messageInput.setSelectionRange(nextCaret, nextCaret);
    mentionMap.set(candidate.label, candidate);
    closeMentionPopup();
    updateComposer();
    messageInput.focus();
  };

  const syncMentionPopup = (): void => {
    const conversation = activeConversation();
    if (!conversation || messageInput.disabled) {
      closeMentionPopup();
      return;
    }
    const caret = messageInput.selectionStart ?? messageInput.value.length;
    const fragment = messageInput.value.slice(0, caret);
    const match = MENTION_PATTERN.exec(fragment);
    if (!match) {
      closeMentionPopup();
      return;
    }
    const query = match[1].toLocaleLowerCase();
    const all = mentionCandidatesFor(conversation);
    mentionCandidates = all.filter((candidate) => candidate.label.toLocaleLowerCase().includes(query));
    if (mentionCandidates.length === 0) {
      closeMentionPopup();
      return;
    }
    mentionAnchor = caret - match[0].length;
    mentionIndex = 0;
    renderMentionPopup();
    mentionPopup.hidden = false;
  };

  const updateComposer = (): void => {
    const conversation = activeConversation();
    let enabled = false;
    let placeholder = '先选择或建立一个会话';
    if (conversation?.kind === 'dm') {
      const peer = store.peers.get(conversation.peerId);
      enabled = peer?.connection === 'connected';
      placeholder = enabled
        ? `发消息给 ${store.peerLabel(conversation.peerId)}，@ 可呼叫对方 Agent`
        : '对方已断开，重新连接后可继续发送';
    } else if (conversation?.kind === 'room') {
      enabled = true;
      placeholder = `发消息到「${conversation.title}」，@ 可呼叫成员的 Agent`;
    }
    messageInput.disabled = !enabled;
    messageInput.placeholder = placeholder;
    attachButton.disabled = !conversation || conversation.kind !== 'room';
    attachButton.title = conversation?.kind === 'room'
      ? '发送文件（不超过 4 MiB）'
      : '直聊暂不支持发送文件，可建群后发送';
    sendButton.disabled = !enabled || messageInput.value.trim().length === 0;
    if (!enabled) closeMentionPopup();
  };

  const senderLabel = (message: { kind: string; isOwn: boolean; senderPeerId: string }): string => {
    if (message.isOwn) {
      return message.kind === 'agent' ? `本机 ${store.localAgentName}` : '我';
    }
    const name = store.peerLabel(message.senderPeerId);
    return message.kind === 'agent' ? `${name} 的 Agent` : name;
  };

  const renderMessage = (conversationId: string, message: ReturnType<NearbyStore['conversationMessages']>[number]): HTMLElement => {
    if (message.kind === 'system') {
      const strip = document.createElement('p');
      strip.className = 'nearby-sysmsg';
      strip.textContent = message.text;
      return strip;
    }
    const item = document.createElement('article');
    item.className = [
      'nearby-message',
      message.isOwn ? 'nearby-message--own' : '',
      message.kind === 'agent' ? 'nearby-message--agent' : '',
      message.isError ? 'nearby-message--error' : '',
    ].filter(Boolean).join(' ');
    item.dataset.messageId = message.id;
    item.dataset.messageType = message.kind;

    const meta = document.createElement('div');
    meta.className = 'nearby-message__meta';
    meta.append(textElement('strong', 'nearby-message__sender', senderLabel(message)));
    if (message.kind === 'agent') meta.append(textElement('span', 'nearby-agent-badge', 'Agent'));
    const time = textElement('time', 'nearby-message__time', messageTime(message.timestamp));
    if (message.timestamp) time.dateTime = new Date(message.timestamp).toISOString();
    meta.append(time);
    item.append(meta);

    if (message.kind === 'file' && message.file) {
      item.append(renderFileCard(message.file, message.isOwn));
      return item;
    }

    const expanded = expandedMessages.has(message.id);
    const tooLong = [...message.text].length > LONG_TEXT_CLAMP;
    const bubble = textElement(
      'p',
      'nearby-message__bubble',
      tooLong && !expanded ? `${[...message.text].slice(0, LONG_TEXT_CLAMP).join('')}…` : message.text,
    );
    item.append(bubble);
    if (tooLong) {
      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'nearby-message__expand';
      toggle.textContent = expanded ? '收起' : '展开全文';
      toggle.addEventListener('click', () => {
        if (expandedMessages.has(message.id)) expandedMessages.delete(message.id);
        else expandedMessages.add(message.id);
        renderMessages(conversationId);
      });
      item.append(toggle);
    }
    return item;
  };

  const renderFileCard = (file: NearbyFileCard, isOwn: boolean): HTMLElement => {
    const card = document.createElement('div');
    card.className = 'nearby-file-card';
    card.append(
      textElement('span', 'nearby-file-card__icon', '📄'),
      (() => {
        const copy = document.createElement('span');
        copy.className = 'nearby-file-card__copy';
        copy.append(
          textElement('strong', 'nearby-file-card__name', file.name),
          textElement('span', 'nearby-file-card__meta', `${formatFileSize(file.size)} · ${file.mime_type}`),
        );
        return copy;
      })(),
    );
    if (file.complete) {
      const save = document.createElement('button');
      save.type = 'button';
      save.className = 'nearby-file-card__save';
      save.textContent = isOwn ? '另存为' : '保存';
      save.addEventListener('click', () => {
        save.disabled = true;
        void Promise.resolve(actions.saveFile(file)).finally(() => {
          if (!disposed) save.disabled = false;
        });
      });
      card.append(save);
    } else {
      card.append(textElement('span', 'nearby-file-card__broken', '传输不完整，无法保存'));
    }
    return card;
  };

  const renderMessages = (conversationId: string): void => {
    messageList.replaceChildren();
    const messages = store.conversationMessages(conversationId);
    const pending = store.pendingAgentSenders(conversationId);
    if (messages.length === 0 && pending.length === 0) {
      messageList.append(textElement('p', 'nearby-chat__messages-empty', '会话已准备好，发一条消息开始交流。'));
    }
    for (const message of messages) messageList.append(renderMessage(conversationId, message));
    for (const peerId of pending) {
      const item = document.createElement('article');
      item.className = 'nearby-message nearby-message--agent nearby-message--thinking';
      const meta = document.createElement('div');
      meta.className = 'nearby-message__meta';
      meta.append(
        textElement('strong', 'nearby-message__sender', `${store.peerLabel(peerId)} 的 Agent`),
        textElement('span', 'nearby-agent-badge', 'Agent'),
      );
      const bubble = document.createElement('p');
      bubble.className = 'nearby-message__bubble nearby-thinking';
      bubble.append(
        textElement('span', 'nearby-thinking__dot', ''),
        textElement('span', 'nearby-thinking__dot', ''),
        textElement('span', 'nearby-thinking__dot', ''),
        textElement('span', 'nearby-thinking__label', '思考中…'),
      );
      item.append(meta, bubble);
      messageList.append(item);
    }
    const count = messages.length + pending.length;
    if (conversationId !== renderedConversationId || count !== renderedMessageCount) {
      messageList.scrollTop = messageList.scrollHeight;
    }
    renderedConversationId = conversationId;
    renderedMessageCount = count;
  };

  const connectionLabel = (conversation: NearbyConversation): string => {
    const peer = store.peers.get(conversation.peerId);
    switch (peer?.connection) {
      case 'connected': return '已连接';
      case 'connecting': return '正在连接';
      case 'unavailable': return '已离开';
      case 'disconnected': return '已断开';
      default: return '未连接';
    }
  };

  const renderHeader = (conversation: NearbyConversation): void => {
    chatAvatar.textContent = conversation.kind === 'room'
      ? '群'
      : (conversation.title.slice(0, 1).toLocaleUpperCase() || '?');
    chatName.textContent = conversation.title;
    if (conversation.kind === 'dm') {
      const peer = store.peers.get(conversation.peerId);
      chatState.textContent = `${store.peerAgentLabel(conversation.peerId)} · ${connectionLabel(conversation)}`;
      chatState.dataset.connection = peer?.connection ?? 'disconnected';
    } else {
      chatState.textContent = `${conversation.memberIds.length} 位成员 · ${AGENT_MODE_LABELS[conversation.agentMode]}`;
      chatState.dataset.connection = 'connected';
    }
    disconnectButton.hidden = conversation.kind !== 'dm';
    membersButton.hidden = conversation.kind !== 'room';
    if (conversation.kind === 'dm') {
      const peer = store.peers.get(conversation.peerId);
      disconnectButton.disabled = peer?.connection !== 'connected';
    }
  };

  const modeOption = (
    conversation: NearbyConversation,
    mode: NearbyAgentMode,
    title: string,
    copy: string,
  ): HTMLElement => {
    const editable = conversation.isOwner;
    const option = document.createElement('label');
    option.className = [
      'nearby-mode-option',
      conversation.agentMode === mode ? 'nearby-mode-option--active' : '',
      editable ? '' : 'nearby-mode-option--readonly',
    ].filter(Boolean).join(' ');
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'nearby-agent-mode';
    radio.checked = conversation.agentMode === mode;
    radio.disabled = !editable;
    radio.addEventListener('change', () => {
      if (radio.checked) actions.setRoomAgentMode(conversation.roomId, mode);
    });
    const copyWrap = document.createElement('span');
    copyWrap.className = 'nearby-mode-option__copy';
    copyWrap.append(
      textElement('strong', 'nearby-mode-option__title', title),
      textElement('span', 'nearby-mode-option__desc', copy),
    );
    option.append(radio, copyWrap);
    return option;
  };

  const resetLeaveConfirm = (): void => {
    confirmLeaveArmed = false;
    if (confirmLeaveTimer) clearTimeout(confirmLeaveTimer);
    confirmLeaveTimer = null;
  };

  const renderPanel = (conversation: NearbyConversation | null): void => {
    const show = Boolean(conversation && conversation.kind === 'room' && panelOpen);
    panelElement.hidden = !show;
    workspace.classList.toggle('nearby-workspace--with-panel', show);
    if (!show || !conversation) return;
    // 群主正在编辑群名时跳过本轮重建，避免输入被远端事件触发的渲染打断
    if (panelElement.querySelector('.nearby-panel__name-input') === document.activeElement) return;
    panelElement.replaceChildren();

    const memberSection = document.createElement('section');
    memberSection.className = 'nearby-panel__section';
    memberSection.append(textElement('h3', 'nearby-panel__title', `成员（${conversation.memberIds.length}）`));
    const memberList = document.createElement('div');
    memberList.className = 'nearby-member-list';
    const orderedMembers = [...conversation.memberIds].sort((a, b) => {
      if (a === store.localPeerId) return -1;
      if (b === store.localPeerId) return 1;
      return store.peerLabel(a).localeCompare(store.peerLabel(b));
    });
    for (const memberId of orderedMembers) {
      const online = memberId === store.localPeerId || store.peers.get(memberId)?.connection === 'connected';
      const row = document.createElement('div');
      row.className = 'nearby-member';
      const dot = textElement('span', 'nearby-member__dot', '');
      dot.dataset.online = online ? 'true' : 'false';
      const name = store.peerLabel(memberId);
      row.append(dot, textElement('span', 'nearby-member__name', memberId === store.localPeerId ? `${name}（我）` : name));
      const agentRow = document.createElement('div');
      agentRow.className = 'nearby-member nearby-member--agent';
      agentRow.append(
        textElement('span', 'nearby-member__name', `└ ${store.peerAgentLabel(memberId)}`),
        textElement('span', 'nearby-agent-badge', 'Agent'),
      );
      memberList.append(row, agentRow);
    }
    memberSection.append(memberList);
    if (conversation.isOwner) {
      const invite = document.createElement('button');
      invite.type = 'button';
      invite.className = 'nearby-secondary-action nearby-panel__invite';
      invite.textContent = '邀请成员';
      const candidates = [...store.peers.values()].filter(
        (peer) => peer.connection === 'connected' && !conversation.memberIds.includes(peer.peer_id),
      );
      invite.disabled = candidates.length === 0;
      invite.title = candidates.length === 0 ? '没有可邀请的已连接同伴' : '邀请已连接的同伴加入本群';
      invite.addEventListener('click', () => openInvitePicker(conversation));
      memberSection.append(invite);
    }
    panelElement.append(memberSection);

    const settingsSection = document.createElement('section');
    settingsSection.className = 'nearby-panel__section';
    settingsSection.append(textElement('h3', 'nearby-panel__title', conversation.isOwner ? '群设置' : '群设置（仅群主可修改）'));
    const nameRow = document.createElement('div');
    nameRow.className = 'nearby-panel__field';
    nameRow.append(textElement('span', 'nearby-panel__label', '群名'));
    if (conversation.isOwner) {
      const nameInput = document.createElement('input');
      nameInput.className = 'nearby-popover__input nearby-panel__name-input';
      nameInput.maxLength = 120;
      nameInput.value = conversation.title;
      nameInput.setAttribute('aria-label', '群名');
      const commitName = (): void => {
        const name = nameInput.value.trim();
        if (name && name !== conversation.title) actions.renameRoom(conversation.roomId, name);
        else nameInput.value = conversation.title;
      };
      nameInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          nameInput.blur();
        } else if (event.key === 'Escape') {
          nameInput.value = conversation.title;
          nameInput.blur();
        }
      });
      nameInput.addEventListener('change', commitName);
      nameRow.append(nameInput);
    } else {
      nameRow.append(textElement('span', 'nearby-panel__value', conversation.title));
    }
    settingsSection.append(nameRow);
    settingsSection.append(textElement('p', 'nearby-panel__label', 'Agent 触发模式'));
    settingsSection.append(
      modeOption(conversation, 'mention', '@触发', '仅被 @ 的 Agent 响应'),
      modeOption(conversation, 'auto', '全员响应', '每条消息所有在场 Agent 都响应（耗 token）'),
      modeOption(conversation, 'quiet', '安静模式', 'Agent 不响应群内消息'),
    );
    panelElement.append(settingsSection);

    const leave = document.createElement('button');
    leave.type = 'button';
    leave.className = 'nearby-danger-action';
    leave.textContent = conversation.isOwner ? '解散群' : '退出群';
    leave.addEventListener('click', () => {
      if (!confirmLeaveArmed) {
        confirmLeaveArmed = true;
        leave.textContent = conversation.isOwner ? '确认解散？' : '确认退出？';
        confirmLeaveTimer = setTimeout(() => {
          resetLeaveConfirm();
          render();
        }, 3_000);
        return;
      }
      resetLeaveConfirm();
      actions.leaveRoom(conversation.roomId);
    });
    panelElement.append(leave);
  };

  const openInvitePicker = (conversation: NearbyConversation): void => {
    const candidates = [...store.peers.values()].filter(
      (peer) => peer.connection === 'connected' && !conversation.memberIds.includes(peer.peer_id),
    );
    const selected = new Set<string>();
    const overlay = document.createElement('div');
    overlay.className = 'nearby-popover-overlay';
    const popover = document.createElement('div');
    popover.className = 'nearby-popover';
    popover.setAttribute('role', 'dialog');
    popover.setAttribute('aria-label', '邀请成员');
    const list = document.createElement('div');
    list.className = 'nearby-popover__list';
    for (const peer of candidates) {
      const row = document.createElement('label');
      row.className = 'nearby-popover__option';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.addEventListener('change', () => {
        if (box.checked) selected.add(peer.peer_id);
        else selected.delete(peer.peer_id);
        confirm.disabled = selected.size === 0;
      });
      row.append(box, textElement('span', 'nearby-popover__option-label', store.peerLabel(peer.peer_id)));
      list.append(row);
    }
    const confirm = document.createElement('button');
    confirm.type = 'button';
    confirm.className = 'nearby-send-button';
    confirm.textContent = '邀请';
    confirm.disabled = true;
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'nearby-secondary-action';
    cancel.textContent = '取消';
    const close = (): void => overlay.remove();
    cancel.addEventListener('click', close);
    overlay.addEventListener('click', (event) => {
      if (event.target === overlay) close();
    });
    confirm.addEventListener('click', () => {
      if (selected.size === 0) return;
      actions.inviteToRoom(conversation.roomId, [...selected]);
      close();
    });
    const footer = document.createElement('div');
    footer.className = 'nearby-popover__footer';
    footer.append(cancel, confirm);
    popover.append(
      textElement('h3', 'nearby-popover__title', `邀请成员加入「${conversation.title}」`),
      list,
      footer,
    );
    overlay.append(popover);
    element.append(overlay);
  };

  const persistDraft = (): void => {
    if (renderedConversationId) drafts.set(renderedConversationId, messageInput.value);
  };

  const restoreDraft = (conversationId: string): void => {
    messageInput.value = drafts.get(conversationId) ?? '';
  };

  function render(): void {
    if (disposed) return;
    const conversation = activeConversation();
    if (conversation?.id !== renderedConversationId) {
      persistDraft();
      mentionMap = new Map();
      closeMentionPopup();
      resetLeaveConfirm();
      if (conversation) {
        panelOpen = conversation.kind === 'room';
        restoreDraft(conversation.id);
      }
      renderedMessageCount = -1;
    }
    emptyState.hidden = Boolean(conversation);
    chat.hidden = !conversation;
    if (!conversation) {
      panelElement.hidden = true;
      workspace.classList.remove('nearby-workspace--with-panel');
      renderedConversationId = null;
      return;
    }
    renderHeader(conversation);
    renderMessages(conversation.id);
    const roomModeHint = conversation.kind === 'room' && conversation.agentMode === 'auto'
      ? '全员响应模式：每条消息都会触发在场 Agent 回复，注意 token 消耗'
      : '';
    if (!composerHint.textContent || roomModeHint) showHint(roomModeHint, true);
    updateComposer();
    renderPanel(conversation);
  }

  messageInput.addEventListener('input', () => {
    updateComposer();
    syncMentionPopup();
  });
  messageInput.addEventListener('click', syncMentionPopup);
  messageInput.addEventListener('keydown', (event) => {
    if (!mentionPopup.hidden) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        mentionIndex = (mentionIndex + (event.key === 'ArrowDown' ? 1 : -1) + mentionCandidates.length) % mentionCandidates.length;
        renderMentionPopup();
        return;
      }
      if (event.key === 'Enter' || event.key === 'Tab') {
        event.preventDefault();
        pickMention(mentionIndex);
        return;
      }
      if (event.key === 'Escape') {
        closeMentionPopup();
        return;
      }
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });
  composer.addEventListener('submit', (event) => {
    event.preventDefault();
    const conversation = activeConversation();
    const text = messageInput.value.trim();
    if (!conversation || !text || messageInput.disabled) return;
    const mentions: NearbyMention[] = [...mentionMap.entries()]
      .filter(([label]) => text.includes(`@${label}`))
      .map(([label, candidate]) => ({ ...candidate, label }));
    messageInput.value = '';
    if (renderedConversationId) drafts.delete(renderedConversationId);
    mentionMap = new Map();
    closeMentionPopup();
    updateComposer();
    actions.sendMessage(text, mentions);
  });
  attachButton.addEventListener('click', () => {
    const conversation = activeConversation();
    if (!conversation || conversation.kind !== 'room') return;
    actions.sendFile();
  });
  disconnectButton.addEventListener('click', () => {
    const conversation = activeConversation();
    if (conversation?.kind === 'dm') actions.disconnectPeer(conversation.peerId);
  });
  membersButton.addEventListener('click', () => {
    panelOpen = !panelOpen;
    renderPanel(activeConversation());
  });

  return {
    element,
    panelElement,
    render,
    dispose(): void {
      disposed = true;
      if (hintTimer) clearTimeout(hintTimer);
      if (confirmLeaveTimer) clearTimeout(confirmLeaveTimer);
    },
    focusComposer(): void {
      if (!messageInput.disabled) messageInput.focus();
    },
  };
}
