function slot<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  label?: string,
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  element.className = className;
  if (label) element.setAttribute('aria-label', label);
  return element;
}

export function createShellTemplate(options: { context?: boolean; inspector?: boolean } = {}) {
  const element = slot('div', 'mw-page-template mw-shell-template');
  const rail = slot('nav', 'mw-shell-template__rail', '主导航');
  const main = slot('main', 'mw-shell-template__main');
  const slots: {
    rail: HTMLElement;
    context?: HTMLElement;
    main: HTMLElement;
    inspector?: HTMLElement;
  } = { rail, main };
  element.dataset.template = 'shell';
  element.dataset.hasContext = String(options.context ?? false);
  element.dataset.hasInspector = String(options.inspector ?? false);
  element.append(rail);
  if (options.context) {
    slots.context = slot('aside', 'mw-shell-template__context', '上下文');
    element.append(slots.context);
  }
  element.append(main);
  if (options.inspector) {
    slots.inspector = slot('aside', 'mw-shell-template__inspector', '检查器');
    element.append(slots.inspector);
  }
  return { element, slots };
}

export function createMasterDetailTemplate() {
  const element = slot('div', 'mw-page-template mw-master-detail');
  const header = slot('header', 'mw-master-detail__header');
  const body = slot('div', 'mw-master-detail__body');
  const master = slot('aside', 'mw-master-detail__master', '列表');
  const detail = slot('main', 'mw-master-detail__detail', '详情');
  element.dataset.template = 'master-detail';
  body.append(master, detail);
  element.append(header, body);
  return { element, slots: { header, master, detail } };
}

/**
 * Shared shell for browseable product entities such as Experts, Agents and
 * Skills. Feature owners populate the slots without redefining page scroll.
 */
export function createHubTemplate() {
  const element = slot('div', 'mw-page-template mw-hub-template');
  const header = slot('header', 'mw-hub-template__header');
  const primaryNavigation = slot('nav', 'mw-hub-template__navigation', '内容类型');
  const filters = slot('section', 'mw-hub-template__filters', '筛选与搜索');
  const results = slot('main', 'mw-hub-template__results', '内容列表');
  element.dataset.template = 'hub';
  results.setAttribute('aria-live', 'polite');
  element.append(header, primaryNavigation, filters, results);
  return { element, slots: { header, primaryNavigation, filters, results } };
}

export function createSettingsTemplate() {
  const element = slot('div', 'mw-page-template mw-settings-template');
  const navigation = slot('nav', 'mw-settings-template__navigation', '设置导航');
  const content = slot('div', 'mw-settings-template__content');
  const form = slot('main', 'mw-settings-template__form');
  const footer = slot('footer', 'mw-settings-template__footer');
  element.dataset.template = 'settings';
  content.append(form, footer);
  element.append(navigation, content);
  return { element, slots: { navigation, form, footer } };
}

export function createBoardTemplate() {
  const element = slot('section', 'mw-page-template mw-board-template');
  const toolbar = slot('header', 'mw-board-template__toolbar');
  const scroller = slot('div', 'mw-board-template__scroller', '任务看板');
  const columns = slot('div', 'mw-board-template__columns');
  element.dataset.template = 'board';
  scroller.setAttribute('role', 'region');
  scroller.append(columns);
  element.append(toolbar, scroller);
  return { element, slots: { toolbar, scroller, columns } };
}

export function createChatWorkspaceTemplate(options: { inspector?: boolean } = {}) {
  const element = slot('div', 'mw-page-template mw-chat-template');
  const rail = slot('nav', 'mw-chat-template__rail', '主导航');
  const history = slot('aside', 'mw-chat-template__history', '对话历史');
  const workspace = slot('main', 'mw-chat-template__workspace');
  const conversation = slot('section', 'mw-chat-template__conversation', '对话');
  const composer = slot('footer', 'mw-chat-template__composer');
  const slots: {
    rail: HTMLElement;
    history: HTMLElement;
    conversation: HTMLElement;
    composer: HTMLElement;
    inspector?: HTMLElement;
  } = { rail, history, conversation, composer };
  element.dataset.template = 'chat-workspace';
  element.dataset.hasInspector = String(options.inspector ?? false);
  conversation.setAttribute('role', 'log');
  conversation.setAttribute('aria-live', 'polite');
  workspace.append(conversation, composer);
  element.append(rail, history, workspace);
  if (options.inspector) {
    slots.inspector = slot('aside', 'mw-chat-template__inspector', '检查器');
    element.append(slots.inspector);
  }
  return { element, slots };
}

export function createInspectorTemplate() {
  const element = slot('aside', 'mw-inspector-template', '检查器');
  const header = slot('header', 'mw-inspector-template__header');
  const tabs = slot('nav', 'mw-inspector-template__tabs', '检查器视图');
  const body = slot('div', 'mw-inspector-template__body', '检查器内容');
  element.dataset.template = 'inspector';
  body.setAttribute('role', 'region');
  element.append(header, tabs, body);
  return { element, slots: { header, tabs, body } };
}
