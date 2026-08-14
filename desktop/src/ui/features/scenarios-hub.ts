/**
 * Welcome start view and Gateway-backed scenario recommendations.
 *
 * The Gateway owns scenario content and execution binding. This owner only
 * renders the start view, exposes loading/fallback state, and reports a picked
 * sub-scenario to the Composer.
 */

import { backendApi, type Scenario, type SubScenario } from '../backend-client';
import { createIcon, MONOCHROME_ICON_CLASS, type IconId } from '../components/icon';

const BATCH = 3;
const SCENARIO_ICONS: readonly IconId[] = [
  'icon-task',
  'icon-agent',
  'icon-search',
  'icon-file',
];

export interface WelcomeCopy {
  title: string;
  subtitle: string;
}

export const WELCOME_COPY_POOL: readonly WelcomeCopy[] = [
  { title: '忙点好，忙点好。', subtitle: '说吧，今天又忙点啥？' },
  { title: '天知地知，你知我知。', subtitle: '你就是最忙的牛马——今天先忙哪件？' },
  { title: '一打开 Crew，我就知道你又有活了。', subtitle: '来吧，先从哪件开始？' },
  { title: '活是一点没少。', subtitle: '不过没事，我陪你一起干。' },
  { title: '来都来了。', subtitle: '咸鱼准备翻身了。' },
  { title: '又见面了，小牛马。', subtitle: '今天先解决哪件麻烦事？' },
];

/** 同一个本地自然日稳定展示同一句，跨日后再轮换。 */
export function welcomeCopyForDate(date = new Date()): WelcomeCopy {
  if (!Number.isFinite(date.getTime())) return WELCOME_COPY_POOL[0];
  const day = Math.floor(Date.UTC(
    date.getFullYear(),
    date.getMonth(),
    date.getDate(),
  ) / 86_400_000);
  return WELCOME_COPY_POOL[((day % WELCOME_COPY_POOL.length) + WELCOME_COPY_POOL.length)
    % WELCOME_COPY_POOL.length];
}

const FALLBACK_SCENARIOS: Scenario[] = [
  {
    id: 'fallback-fin',
    title: '金融服务',
    description: '资讯检索、财报与竞品分析',
    items: [
      { id: 'fin-report', title: '竞品财报摘要', query: '帮我分析竞品最新财报的核心要点与风险提示' },
      { id: 'fin-news', title: '行业资讯速览', query: '检索今日金融行业重要资讯并给出摘要' },
    ],
  },
  {
    id: 'fallback-doc',
    title: '文档处理',
    description: 'Markdown / Word / PDF 互转与精排',
    items: [
      { id: 'doc-md-pdf', title: 'MD 转 PDF', query: '帮我把这份 Markdown 文档转成排版精美的 PDF' },
      { id: 'doc-ppt', title: '一键生成 PPT', query: '根据大纲生成演示文稿结构与每页要点' },
    ],
  },
  {
    id: 'fallback-mail',
    title: '邮箱办公',
    description: '邮件撰写、收发与摘要',
    items: [
      { id: 'mail-draft', title: '撰写回复', query: '帮我写一封专业、简洁的邮件回复' },
      { id: 'mail-summary', title: '邮件摘要', query: '总结这封邮件的核心诉求与待办事项' },
    ],
  },
];

let scenarios: Scenario[] = [...FALLBACK_SCENARIOS];
let activeId: string | null = null;
let onPick: ((sub: SubScenario, parent: Scenario) => void) | null = null;
let loadGeneration = 0;
let outsideClickHandler: ((event: MouseEvent) => void) | null = null;
let composerPopoverOpenHandler: (() => void) | null = null;

function mountWelcomeView(): void {
  const host = document.getElementById('welcome-panel');
  if (!host || host.querySelector('[data-welcome-view]')) return;

  const view = document.createElement('div');
  const header = document.createElement('header');
  const identity = document.createElement('div');
  const mascot = document.createElement('div');
  const logo = document.createElement('img');
  const heading = document.createElement('div');
  const title = document.createElement('h1');
  const subtitle = document.createElement('p');
  const scenariosSection = document.createElement('section');
  const sectionHeader = document.createElement('header');
  const sectionTitle = document.createElement('h2');
  const refresh = document.createElement('button');
  const status = document.createElement('div');
  const cards = document.createElement('div');
  const items = document.createElement('div');

  host.className = 'welcome-view-host';
  view.className = 'welcome-view';
  view.dataset.welcomeView = '';
  header.className = 'welcome-view__header';
  identity.className = 'welcome-view__identity';
  mascot.className = 'welcome-view__mascot';
  mascot.setAttribute('aria-hidden', 'true');
  logo.className = 'welcome-view__logo';
  logo.src = './icon.png';
  logo.alt = '';
  logo.decoding = 'async';
  heading.className = 'welcome-view__heading';
  const welcomeCopy = welcomeCopyForDate();
  title.className = 'welcome-view__title';
  title.textContent = welcomeCopy.title;
  subtitle.className = 'welcome-view__subtitle';
  subtitle.textContent = welcomeCopy.subtitle;
  mascot.append(logo);
  heading.append(title, subtitle, mascot);
  identity.append(heading);
  header.append(identity);

  scenariosSection.className = 'welcome-scenarios';
  scenariosSection.setAttribute('aria-labelledby', 'welcome-scenarios-title');
  sectionHeader.className = 'welcome-scenarios__header';
  sectionTitle.id = 'welcome-scenarios-title';
  sectionTitle.className = 'welcome-scenarios__title';
  sectionTitle.textContent = '快捷场景';
  refresh.id = 'scenario-refresh';
  refresh.type = 'button';
  refresh.className = 'mw-button mw-button--ghost mw-button--icon welcome-scenarios__refresh';
  refresh.title = '换一换';
  refresh.setAttribute('aria-label', '换一换');
  refresh.append(createIcon('icon-refresh', { size: 16 }));
  sectionHeader.append(sectionTitle, refresh);
  status.id = 'scenario-status';
  status.className = 'welcome-scenarios__status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  cards.id = 'scenario-cards';
  cards.className = 'welcome-scenarios__commands';
  cards.setAttribute('aria-label', '推荐场景列表');
  items.id = 'scenario-items';
  items.className = 'welcome-scenarios__items';
  items.setAttribute('aria-label', '场景操作');
  items.hidden = true;
  scenariosSection.append(sectionHeader, status, cards, items);
  view.append(header, scenariosSection);
  host.replaceChildren(view);
}

function createScenarioCommand(scenario: Scenario, index: number): HTMLButtonElement {
  const button = document.createElement('button');
  const symbol = document.createElement('span');
  const title = document.createElement('strong');
  button.type = 'button';
  button.className = 'scenario-command';
  button.dataset.scenarioId = scenario.id;
  button.title = scenario.description || scenario.title;
  button.setAttribute('aria-expanded', scenario.id === activeId ? 'true' : 'false');
  button.setAttribute('aria-controls', 'scenario-items');
  symbol.className = 'scenario-command__symbol';
  const icon = SCENARIO_ICONS[index % SCENARIO_ICONS.length];
  const iconOptions = icon === 'icon-agent'
    ? { size: 18 as const, className: MONOCHROME_ICON_CLASS }
    : { size: 18 as const };
  symbol.append(createIcon(icon, iconOptions));
  title.className = 'scenario-command__title';
  title.textContent = scenario.title;
  button.append(symbol, title);
  return button;
}

function renderCards(): void {
  const host = document.getElementById('scenario-cards');
  if (!host) return;
  host.replaceChildren(...scenarios.map(createScenarioCommand));
}

function syncCardState(): void {
  document.querySelectorAll<HTMLButtonElement>('[data-scenario-id]').forEach((button) => {
    const active = button.dataset.scenarioId === activeId;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-expanded', active ? 'true' : 'false');
  });
}

function collapseScenarioItems(): void {
  if (!activeId) return;
  activeId = null;
  syncCardState();
  renderItems();
}

function renderItems(): void {
  const host = document.getElementById('scenario-items');
  if (!host) return;
  const active = scenarios.find((scenario) => scenario.id === activeId);
  host.replaceChildren();
  if (!active?.items.length) {
    host.hidden = true;
    return;
  }
  for (const item of active.items) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'scenario-item';
    button.dataset.subId = item.id;
    button.title = item.query;
    button.textContent = item.title;
    host.append(button);
  }
  host.hidden = false;
}

function setLoadState(state: 'loading' | 'ready' | 'fallback', text: string): void {
  const status = document.getElementById('scenario-status');
  const refresh = document.getElementById('scenario-refresh') as HTMLButtonElement | null;
  if (status) {
    status.dataset.state = state;
    status.textContent = text;
  }
  if (refresh) refresh.disabled = state === 'loading';
}

function applyScenarios(list: Scenario[]): void {
  const valid = list.filter((scenario) => scenario && Array.isArray(scenario.items));
  scenarios = valid.length > 0 ? valid : [...FALLBACK_SCENARIOS];
  if (activeId && !scenarios.some((scenario) => scenario.id === activeId)) activeId = null;
  renderCards();
  renderItems();
}

async function load(): Promise<void> {
  collapseScenarioItems();
  const generation = ++loadGeneration;
  setLoadState('loading', '正在加载推荐场景…');
  try {
    const list = await backendApi.scenarios(BATCH);
    if (generation !== loadGeneration) return;
    applyScenarios(list);
    setLoadState('ready', `已加载 ${scenarios.length} 个推荐场景`);
  } catch {
    if (generation !== loadGeneration) return;
    applyScenarios([]);
    setLoadState('fallback', '服务暂不可用，当前显示本地推荐');
  }
}

/**
 * Mounts the start view and binds scenario selection to the Composer owner.
 */
export function bindScenarioHub(pick: (sub: SubScenario, parent: Scenario) => void): void {
  mountWelcomeView();
  scenarios = [...FALLBACK_SCENARIOS];
  activeId = null;
  onPick = pick;
  renderCards();
  renderItems();

  document.getElementById('scenario-refresh')?.addEventListener('click', () => void load());
  document.getElementById('scenario-cards')?.addEventListener('click', (event) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-scenario-id]');
    if (!button) return;
    const id = button.dataset.scenarioId || null;
    activeId = activeId === id ? null : id;
    syncCardState();
    renderItems();
  });
  document.getElementById('scenario-items')?.addEventListener('click', (event) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-sub-id]');
    const parent = scenarios.find((scenario) => scenario.id === activeId);
    const item = parent?.items.find((candidate) => candidate.id === button?.dataset.subId);
    if (parent && item) {
      onPick?.(item, parent);
      collapseScenarioItems();
    }
  });
  if (outsideClickHandler) document.removeEventListener('click', outsideClickHandler);
  outsideClickHandler = (event) => {
    // 点场景按钮（切换展开）或子项（选择指令）交给各自处理器；其余任意点击（含空白处）都收起
    const target = event.target as HTMLElement;
    if (!target.closest('[data-scenario-id]') && !target.closest('[data-sub-id]')) {
      collapseScenarioItems();
    }
  };
  document.addEventListener('click', outsideClickHandler);
  if (composerPopoverOpenHandler) {
    window.removeEventListener('composer:popover-opened', composerPopoverOpenHandler);
  }
  composerPopoverOpenHandler = collapseScenarioItems;
  window.addEventListener('composer:popover-opened', composerPopoverOpenHandler);

  void load();
}

/** Refreshes recommendations after the Gateway reconnects. */
export function refreshScenarioHub(): void {
  void load();
}
