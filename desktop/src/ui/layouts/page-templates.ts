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
