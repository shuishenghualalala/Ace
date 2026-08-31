import { conversationAdapters, type ConversationAgentPresence } from './conversation-adapters';
import { insertConversationAgentMention } from './composer-mention';
import { sessionStore } from '../stores/session-store';
import { createAvatarElement } from '../avatar-manager';

const expandedSessions = new Set<string>();

function agentButton(agent: ConversationAgentPresence, compact = false): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = compact
    ? 'companion-presence__quick-agent'
    : 'companion-presence__agent';
  button.dataset.state = agent.state;
  button.disabled = Boolean(agent.disabledReason) || agent.state === 'local';
  button.title = button.disabled
    ? agent.disabledReason || `${agent.label} 是本机 Agent`
    : `@${agent.label}`;
  button.setAttribute('aria-label', `${agent.label}，${agent.stateLabel}，主人 ${agent.ownerLabel}`);
  const avatar = createAvatarElement({
    kind: 'companion-agent',
    id: agent.publicAgentId,
    name: agent.label,
    avatar: agent.avatar,
  }, { className: 'companion-presence__avatar', size: 20 });
  const dot = document.createElement('span');
  dot.className = 'companion-presence__dot';
  dot.setAttribute('aria-hidden', 'true');
  avatar.append(dot);
  button.append(avatar);
  if (!compact) {
    const copy = document.createElement('span');
    copy.className = 'companion-presence__agent-copy';
    const name = document.createElement('strong');
    name.textContent = agent.label;
    const status = document.createElement('small');
    status.textContent = agent.stateLabel;
    copy.append(name, status);
    button.append(copy);
  }
  if (!button.disabled) button.addEventListener('click', () => insertConversationAgentMention(agent));
  return button;
}

export function createCompanionComposerPresence(
  host: HTMLElement,
  getSessionId: () => string | null,
): { refresh(): void; dispose(): void } {
  const root = document.createElement('section');
  root.className = 'companion-presence';
  root.hidden = true;
  host.prepend(root);
  let disposed = false;
  let subscribedAdapterId = '';
  let unsubscribeAdapter: (() => void) | null = null;

  const bindAdapter = (sessionId: string): void => {
    const adapter = conversationAdapters.resolve(sessionId);
    if (adapter?.id === subscribedAdapterId) return;
    unsubscribeAdapter?.();
    unsubscribeAdapter = null;
    subscribedAdapterId = adapter?.id || '';
    if (adapter?.subscribe) unsubscribeAdapter = adapter.subscribe(() => render());
  };

  const render = (): void => {
    if (disposed) return;
    const sessionId = getSessionId();
    if (!sessionId) {
      root.hidden = true;
      return;
    }
    bindAdapter(sessionId);
    const context = conversationAdapters.resolve(sessionId)?.composerContext?.(sessionId) ?? null;
    if (!context) {
      root.hidden = true;
      return;
    }
    root.hidden = false;
    const expanded = expandedSessions.has(sessionId);
    root.dataset.expanded = String(expanded);
    root.replaceChildren();

    const header = document.createElement('div');
    header.className = 'companion-presence__header';
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'companion-presence__toggle';
    toggle.setAttribute('aria-expanded', String(expanded));
    toggle.setAttribute('aria-label', `${expanded ? '收起' : '展开'}群成员状态`);
    const onlineCount = context.members.filter((member) => member.state === 'online').length;
    const agents = context.members.flatMap((member) => member.agents);
    const availableCount = agents.filter((agent) => agent.state === 'available' || agent.state === 'working').length;
    const summary = document.createElement('span');
    summary.className = 'companion-presence__summary';
    const title = document.createElement('strong');
    title.textContent = context.title;
    const detail = document.createElement('span');
    detail.textContent = `${onlineCount}/${context.members.length} 人在线 · ${availableCount} 个 Agent 可用 · ${context.modeLabel}`;
    const chevron = document.createElement('span');
    chevron.className = 'companion-presence__chevron';
    chevron.setAttribute('aria-hidden', 'true');
    summary.append(title, detail);
    toggle.append(summary, chevron);
    toggle.addEventListener('click', () => {
      if (expandedSessions.has(sessionId)) expandedSessions.delete(sessionId);
      else expandedSessions.add(sessionId);
      render();
    });
    const quick = document.createElement('div');
    quick.className = 'companion-presence__quick';
    quick.setAttribute('aria-label', '可 @ 的群内 Agent');
    for (const agent of agents.filter((item) => !item.disabledReason && item.state !== 'local').slice(0, 5)) {
      quick.append(agentButton(agent, true));
    }
    header.append(toggle, quick);
    root.append(header);

    if (!expanded) return;
    const members = document.createElement('div');
    members.className = 'companion-presence__members';
    members.setAttribute('role', 'list');
    for (const member of context.members) {
      const row = document.createElement('article');
      row.className = 'companion-presence__member';
      row.dataset.state = member.state;
      row.setAttribute('role', 'listitem');
      const identity = document.createElement('div');
      identity.className = 'companion-presence__identity';
      const avatar = createAvatarElement({
        kind: 'companion-user',
        id: member.peerId,
        name: member.label,
        avatar: member.avatar,
      }, { className: 'companion-presence__avatar', size: 24 });
      const dot = document.createElement('span');
      dot.className = 'companion-presence__dot';
      dot.setAttribute('aria-hidden', 'true');
      avatar.append(dot);
      const copy = document.createElement('span');
      const name = document.createElement('strong');
      name.textContent = member.label;
      const status = document.createElement('small');
      status.textContent = member.stateLabel;
      copy.append(name, status);
      identity.append(avatar, copy);
      const agentList = document.createElement('div');
      agentList.className = 'companion-presence__agents';
      if (member.agents.length === 0) {
        const empty = document.createElement('small');
        empty.className = 'companion-presence__no-agent';
        empty.textContent = '未公开 Agent';
        agentList.append(empty);
      } else {
        for (const agent of member.agents) agentList.append(agentButton(agent));
      }
      row.append(identity, agentList);
      members.append(row);
    }
    root.append(members);
  };

  const unsubscribeSession = sessionStore.subscribe((next, previous) => {
    if (next.activeSessionId !== previous.activeSessionId) render();
  });
  render();
  return {
    refresh: render,
    dispose() {
      disposed = true;
      unsubscribeAdapter?.();
      unsubscribeSession();
      root.remove();
    },
  };
}
