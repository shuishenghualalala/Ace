import { createBadge, createButton, createTabs } from '../components/controls';
import { createIcon } from '../components/icon';
import { createHubTemplate } from '../layouts/page-templates';
import { externalAgentInitial, externalAgentTone } from './external-agent-avatar';

export type AgentHubTab = 'mine' | 'runtime' | 'create-agent' | 'create-team';

export interface AgentHubAgent {
  id: string;
  name: string;
  provider: string;
  displayBadge?: string;
  detail: string;
  tags: string[];
  available: boolean;
}

export interface AgentHubTeam {
  id: string;
  name: string;
  description: string;
  memberCount: number;
  available: boolean;
}

export interface AgentHubRuntime {
  id: string;
  name: string;
  provider: string;
  detail: string;
  statusDetail?: string;
  availability: 'ready' | 'degraded' | 'unavailable';
}

export interface AgentHubState {
  tab: AgentHubTab;
  agents: AgentHubAgent[];
  teams: AgentHubTeam[];
  runtimes: AgentHubRuntime[];
  loading?: boolean;
  message?: string;
  featureEnabled?: boolean;
  scanning?: boolean;
  form?: HTMLElement;
}

export interface AgentHubOptions {
  state: AgentHubState;
  onTabChange?: (tab: AgentHubTab) => void;
  onUseAgent?: (id: string) => void;
  onDeleteAgent?: (id: string) => void;
  onUseTeam?: (id: string) => void;
  onDeleteTeam?: (id: string) => void;
  onUseRuntime?: (id: string) => void;
  onDeleteRuntime?: (id: string) => void;
  onScanRuntimes?: () => void;
  onOpenGuide?: () => void;
}

export interface AgentHubView {
  element: HTMLElement;
  update(state: AgentHubState): void;
  dispose(): void;
}

const runtimeLabels = {
  ready: '随时可用',
  degraded: '模型探测失败',
  unavailable: '暂时不可用',
} as const;

function textElement(tag: string, className: string, text: string): HTMLElement {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
}

function createExternalAgentAvatar(provider: string, displayBadge?: string): HTMLElement {
  const avatar = document.createElement('span');
  const initial = document.createElement('span');
  avatar.className = `mw-agent-card__external-avatar agent-provider-tone-${externalAgentTone(provider)}`;
  avatar.setAttribute('aria-hidden', 'true');
  initial.textContent = externalAgentInitial(provider, displayBadge);
  avatar.append(initial);
  return avatar;
}

function createTeamLogo(): HTMLElement {
  const logo = document.createElement('span');
  logo.className = 'session__team-logo';
  logo.setAttribute('aria-hidden', 'true');
  logo.append(document.createElement('i'), document.createElement('i'));
  return logo;
}

function action(
  label: string,
  dataName: string,
  id: string,
  onPress: (id: string) => void,
  variant: 'primary' | 'secondary' | 'danger' = 'secondary',
  disabled = false,
): HTMLButtonElement {
  const control = createButton({
    label,
    variant,
    size: 'small',
    disabled,
    onPress: () => onPress(id),
  });
  control.element.dataset[dataName] = id;
  return control.element;
}

/** Owns the shared Agent Hub shell while agents-page keeps backend workflows. */
export function createAgentHubView(options: AgentHubOptions): AgentHubView {
  const template = createHubTemplate();
  const heading = document.createElement('div');
  const actions = document.createElement('div');
  const message = document.createElement('div');
  const results = document.createElement('div');
  const tabs = createTabs({
    label: '智能体视图',
    value: options.state.tab,
    items: [
      { id: 'mine', label: '我的阵容' },
      { id: 'runtime', label: '发现外援' },
      { id: 'create-agent', label: '添加外援' },
      { id: 'create-team', label: '组建团队' },
    ],
    onChange: (id) => options.onTabChange?.(id as AgentHubTab),
  });
  let current = options.state;

  template.element.classList.add('mw-agent-hub');
  heading.className = 'mw-hub-heading';
  heading.append(
    textElement('h1', 'mw-hub-heading__title', '外援'),
    textElement('p', 'mw-hub-heading__description', '发现电脑里的 AI 帮手，加入阵容、直接派活，或者拉上他们一起组队。'),
  );
  actions.className = 'mw-hub-actions';
  if (options.onOpenGuide) {
    const guide = document.createElement('button');
    guide.type = 'button';
    guide.className = 'agents-guide-replay';
    guide.dataset.agentsGuideOpen = '';
    guide.title = '重新查看外援引导';
    guide.setAttribute('aria-label', '重新查看外援引导');
    guide.textContent = '?';
    guide.addEventListener('click', options.onOpenGuide);
    actions.append(guide);
  }
  for (const tab of tabs.element.querySelectorAll<HTMLButtonElement>('[data-tab-id]')) {
    tab.dataset.agentsTab = tab.dataset.tabId;
  }
  message.className = 'mw-agent-hub__message';
  message.setAttribute('role', 'status');
  results.className = 'mw-agent-hub__results';
  template.slots.header.append(heading, actions);
  template.slots.primaryNavigation.append(tabs.element);
  template.slots.filters.append(message);
  template.slots.results.append(results);

  const renderMine = (): void => {
    const section = (title: string, count: number): HTMLElement => {
      const header = document.createElement('header');
      header.className = 'mw-agent-hub__section-header';
      header.append(textElement('h2', '', title), createBadge({ label: `${count} 个`, compact: true }));
      return header;
    };
    const agentSection = document.createElement('section');
    const teamSection = document.createElement('section');
    const agentGrid = document.createElement('div');
    const teamGrid = document.createElement('div');
    agentSection.className = 'mw-agent-hub__section';
    teamSection.className = 'mw-agent-hub__section';
    agentGrid.className = 'mw-agent-hub__grid';
    teamGrid.className = 'mw-agent-hub__grid';
    agentSection.append(section('我的外援', current.agents.length), agentGrid);
    teamSection.append(section('我的团队', current.teams.length), teamGrid);

    for (const agent of current.agents) {
      const card = document.createElement('article');
      const copy = document.createElement('div');
      const tags = document.createElement('div');
      const cardActions = document.createElement('div');
      card.className = 'mw-agent-card';
      card.dataset.agentId = agent.id;
      copy.className = 'mw-agent-card__copy';
      copy.append(
        textElement('h3', 'mw-agent-card__title', agent.name),
        textElement('p', 'mw-agent-card__detail', agent.detail),
      );
      tags.className = 'mw-agent-card__tags';
      tags.append(createBadge({ label: agent.provider, compact: true }));
      for (const tag of agent.tags.slice(0, 3)) tags.append(createBadge({ label: tag, compact: true }));
      copy.append(tags);
      cardActions.className = 'mw-agent-card__actions';
      cardActions.append(
        action('派活', 'useAgent', agent.id, (id) => options.onUseAgent?.(id), 'primary', !agent.available),
        action('删除', 'deleteAgent', agent.id, (id) => options.onDeleteAgent?.(id), 'danger'),
      );
      card.append(createExternalAgentAvatar(agent.provider, agent.displayBadge), copy, cardActions);
      agentGrid.append(card);
    }
    if (!current.agents.length) agentGrid.append(textElement('p', 'mw-hub-state', '阵容还是空的'));

    for (const team of current.teams) {
      const card = document.createElement('article');
      const copy = document.createElement('div');
      const cardActions = document.createElement('div');
      card.className = 'mw-agent-card';
      card.dataset.teamId = team.id;
      copy.className = 'mw-agent-card__copy';
      copy.append(
        textElement('h3', 'mw-agent-card__title', team.name),
        textElement('p', 'mw-agent-card__detail', team.description),
        createBadge({ label: `${team.memberCount} 名成员`, compact: true }),
      );
      cardActions.className = 'mw-agent-card__actions';
      cardActions.append(
        action('派活', 'useTeam', team.id, (id) => options.onUseTeam?.(id), 'primary', !team.available),
        action('删除', 'deleteTeam', team.id, (id) => options.onDeleteTeam?.(id), 'danger'),
      );
      card.append(createTeamLogo(), copy, cardActions);
      teamGrid.append(card);
    }
    if (!current.teams.length) teamGrid.append(textElement('p', 'mw-hub-state', '还没有小队'));
    results.append(agentSection, teamSection);
  };

  const renderRuntimes = (): void => {
    const header = document.createElement('header');
    const list = document.createElement('div');
    const scan = createButton({
      label: current.scanning ? '正在找…' : '再找找',
      icon: 'icon-refresh',
      variant: 'secondary',
      loading: Boolean(current.scanning),
      ...(options.onScanRuntimes ? { onPress: options.onScanRuntimes } : {}),
    });
    scan.element.dataset.scanRuntimes = '';
    header.className = 'mw-agent-hub__section-header';
    header.append(textElement('h2', '', '发现外援'), scan.element);
    list.className = 'mw-agent-hub__runtime-list';
    results.append(header, list);
    for (const runtime of current.runtimes) {
      const card = document.createElement('article');
      const copy = document.createElement('div');
      const cardActions = document.createElement('div');
      const availability = createBadge({
        label: runtimeLabels[runtime.availability],
        tone: runtime.availability === 'ready' ? 'success' : runtime.availability === 'degraded' ? 'warning' : 'danger',
        compact: true,
      });
      card.className = 'mw-agent-card mw-agent-card--runtime';
      card.dataset.runtimeId = runtime.id;
      copy.className = 'mw-agent-card__copy';
      copy.append(
        textElement('h3', 'mw-agent-card__title', runtime.name || runtime.provider),
        textElement('p', 'mw-agent-card__detail', runtime.detail),
      );
      if (runtime.statusDetail) {
        copy.append(textElement('p', 'mw-agent-card__runtime-status-detail', runtime.statusDetail));
      }
      availability.classList.add('mw-agent-card__runtime-status');
      cardActions.className = 'mw-agent-card__runtime-actions';
      cardActions.append(
        action(
          '使用',
          'useRuntime',
          runtime.id,
          (id) => options.onUseRuntime?.(id),
          'primary',
          runtime.availability !== 'ready',
        ),
      );
      if (options.onDeleteRuntime) {
        cardActions.append(action(
          '删除',
          'deleteRuntime',
          runtime.id,
          options.onDeleteRuntime,
          'danger',
        ));
      }
      card.append(
        createIcon('icon-external-agent', { size: 32 }),
        copy,
        availability,
        cardActions,
      );
      list.append(card);
    }
    if (!current.runtimes.length) {
      list.append(textElement('p', 'mw-hub-state', '这次还没找到外援。确认已安装支持的 AI 工具后，再找找。'));
    }
  };

  const render = (): void => {
    tabs.setValue(current.tab);
    message.textContent = current.message ?? '';
    message.hidden = !current.message;
    results.replaceChildren();
    results.dataset.tab = current.tab;
    if (current.featureEnabled === false) {
      results.append(textElement('p', 'mw-hub-state', '外援功能暂未开放，请联系管理员开启。'));
      return;
    }
    if (current.loading) {
      results.append(textElement('p', 'mw-hub-state', '正在加载外援…'));
      return;
    }
    if (current.tab === 'mine') renderMine();
    else if (current.tab === 'runtime') renderRuntimes();
    else {
      const formRegion = document.createElement('section');
      formRegion.className = 'mw-agent-hub__form';
      formRegion.dataset.agentFormRegion = '';
      if (current.form) formRegion.append(current.form);
      else formRegion.append(textElement('p', 'mw-hub-state', '表单正在准备…'));
      results.append(formRegion);
    }
  };

  render();
  return {
    element: template.element,
    update(state) {
      current = state;
      render();
    },
    dispose() {
      tabs.dispose();
    },
  };
}
