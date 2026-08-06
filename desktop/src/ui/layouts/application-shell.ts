import { createIcon } from '../components/icon';
import { openPopover, type OverlayHandle } from '../components/overlays';
import {
  resolveShellNavigation,
  type FeatureState,
  type ShellFeatureStates,
  type ShellLocation,
} from '../features/sidebar-nav';
import {
  productModeStore,
  restoreProductMode,
  setProductMode,
  updateProductModeView,
  type ProductMode,
} from '../stores/product-mode-store';
import { createShellTemplate } from './page-templates';

const PRODUCT_COPY: Record<ProductMode, { label: string; description: string }> = {
  assistant: {
    label: 'Crew',
    description: '通用对话、Agent 与项目',
  },
  work: {
    label: 'Crew 办公助手',
    description: '事项、文件与办公知识',
  },
};

export interface ApplicationShellCommands {
  minimize?: () => void | Promise<void>;
  maximize?: () => void | Promise<void>;
  close?: () => void | Promise<void>;
  update?: () => void | Promise<void>;
  settings?: () => void | Promise<void>;
  newChat?: () => void | Promise<void>;
}

export interface ApplicationShellOptions {
  commands?: ApplicationShellCommands;
  features?: ShellFeatureStates;
  storage?: Storage;
  productIconUrl?: string;
  onNavigate?: (location: ShellLocation, productMode: ProductMode) => boolean | void;
  onProductModeChange?: (productMode: ProductMode) => void;
}

export interface ApplicationShell {
  element: HTMLDivElement;
  slots: {
    context: HTMLElement;
    contextContent: HTMLDivElement;
    historyActions: HTMLDivElement;
    page: HTMLElement;
  };
  setFeatures(features: ShellFeatureStates): void;
  setProductMode(productMode: ProductMode): void;
  dispose(): void;
}

function createCommandButton(
  command: keyof ApplicationShellCommands | 'collapse',
  label: string,
  icon: Parameters<typeof createIcon>[0],
): HTMLButtonElement {
  const button = document.createElement('button');
  const text = document.createElement('span');
  button.type = 'button';
  button.className = 'mw-app-command';
  button.dataset.shellCommand = command;
  const commandIds: Partial<Record<keyof ApplicationShellCommands | 'collapse', string>> = {
    update: 'version-update-sidebar-btn',
    settings: 'settings-btn',
  };
  const id = commandIds[command];
  if (id) button.id = id;
  button.title = label;
  button.setAttribute('aria-label', label);
  text.className = 'mw-app-command__label';
  text.textContent = label;
  button.append(createIcon(icon, { className: 'mw-app-command__icon' }), text);
  return button;
}

function createWindowButton(
  command: 'minimize' | 'maximize' | 'close',
  label: string,
  glyph: string,
): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `mw-window-command mw-window-command--${command}`;
  button.dataset.shellCommand = command;
  button.id = `title-bar-${command}`;
  button.title = label;
  button.setAttribute('aria-label', label);
  button.textContent = glyph;
  return button;
}

/**
 * Creates the shared application chrome without owning any feature-page DOM.
 *
 * The returned context and page slots are the only attachment points used by
 * later vertical slices. Product switching updates shell state only.
 */
export function createApplicationShell(
  options: ApplicationShellOptions = {},
): ApplicationShell {
  const storage = options.storage ?? localStorage;
  let features = options.features ?? {};
  let productMenu: OverlayHandle | null = null;
  let disposed = false;

  restoreProductMode(storage);

  const element = document.createElement('div');
  const titlebar = document.createElement('header');
  const productTrigger = document.createElement('button');
  const productIcon = document.createElement('img');
  const productLabel = document.createElement('span');
  const windowCommands = document.createElement('div');
  const template = createShellTemplate({ context: true });
  const navigation = document.createElement('div');
  const navigationList = document.createElement('nav');
  const navigationFooter = document.createElement('div');
  const contextHeader = document.createElement('header');
  const contextTitle = document.createElement('strong');
  const contextContent = document.createElement('div');
  const historyActions = document.createElement('div');

  element.className = 'mw-application-shell';
  element.dataset.applicationShell = '';
  titlebar.id = 'title-bar';
  titlebar.className = 'mw-app-titlebar';
  titlebar.dataset.shellTitlebar = '';
  productTrigger.type = 'button';
  productTrigger.className = 'mw-product-mode-trigger';
  // ponytail: 办公助手 (work) 模式已移除——左上角不再是模式切换下拉，仅作静态品牌标识。
  productIcon.className = 'mw-product-mode-trigger__icon';
  productIcon.src = options.productIconUrl ?? './icon.png';
  productIcon.alt = 'Crew';
  productLabel.className = 'mw-product-mode-trigger__label';
  productTrigger.append(productIcon, productLabel);

  windowCommands.className = 'mw-window-commands';
  windowCommands.append(
    createWindowButton('minimize', '最小化', '−'),
    createWindowButton('maximize', '最大化或还原', '□'),
    createWindowButton('close', '关闭', '×'),
  );
  titlebar.append(productTrigger, windowCommands);

  template.element.classList.add('mw-application-shell__body');
  template.slots.rail.classList.add('mw-app-rail');
  template.slots.context?.classList.add('mw-app-context');
  template.slots.main.classList.add('mw-app-page-outlet');
  template.slots.main.dataset.shellPageOutlet = '';
  navigation.className = 'mw-app-navigation';
  navigationList.className = 'mw-app-navigation__list';
  navigationList.setAttribute('aria-label', '主导航');
  navigationFooter.className = 'mw-app-navigation__footer';
  const contextCollapseCommand = createCommandButton('collapse', '收起上下文', 'icon-panel-collapse');
  contextCollapseCommand.classList.add('mw-app-context__collapse');
  const restoreContextCommand = createCommandButton(
    'collapse',
    '展开工作上下文',
    'icon-panel-collapse',
  );
  restoreContextCommand.classList.add('mw-app-context-restore');
  restoreContextCommand.hidden = true;
  const historyCollapseCommand = createCommandButton('collapse', '收起历史', 'icon-panel-collapse');
  historyCollapseCommand.classList.add('mw-chat-history-actions__toggle');
  historyCollapseCommand.id = 'history-panel-toggle';
  const newChatCommand = createCommandButton('newChat', '新建对话', 'icon-chat-new');
  newChatCommand.classList.add('mw-chat-history-actions__new-chat');
  newChatCommand.id = 'history-collapsed-new-chat';
  const updateCommand = createCommandButton('update', '检查更新', 'icon-refresh');
  const updatePercent = document.createElement('span');
  const settingsCommand = createCommandButton('settings', '设置', 'icon-settings');
  updateCommand.hidden = true;
  updatePercent.id = 'version-update-percent';
  updatePercent.className = 'mw-app-command__progress';
  updatePercent.hidden = true;
  updateCommand.append(updatePercent);
  navigationFooter.append(
    updateCommand,
    settingsCommand,
  );
  navigation.append(navigationList, navigationFooter);
  template.slots.rail.append(navigation);

  contextHeader.className = 'mw-app-context__header';
  contextTitle.className = 'mw-app-context__title';
  contextContent.className = 'mw-app-context__content';
  contextContent.dataset.shellContextOutlet = '';
  historyActions.className = 'mw-chat-history-actions';
  historyActions.dataset.shellHistoryActions = '';
  historyActions.append(historyCollapseCommand, newChatCommand);
  contextHeader.append(contextTitle, contextCollapseCommand, historyActions);
  template.slots.context?.append(contextHeader, contextContent);
  template.element.append(restoreContextCommand);
  element.append(titlebar, template.element);

  const closeProductMenu = (): void => {
    productMenu?.close();
    productMenu = null;
  };

  const switchProductMode = (productMode: ProductMode): void => {
    closeProductMenu();
    if (productModeStore.get().productMode === productMode) return;
    setProductMode(productMode, storage);
    options.onProductModeChange?.(productMode);
    const state = productModeStore.get();
    const item = resolveShellNavigation(productMode, features).find(
      (candidate) =>
        candidate.id === state.views[productMode].lastPosition &&
        candidate.featureState === 'available',
    );
    if (item) options.onNavigate?.(item.id, productMode);
  };

  const openProductMenu = (): void => {
    if (productMenu) {
      closeProductMenu();
      return;
    }
    const menu = document.createElement('div');
    menu.className = 'mw-product-mode-menu';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-label', '切换助手模式');
    const current = productModeStore.get().productMode;
    // ponytail: 办公助手 (work) 模式已移除，下拉菜单只保留通用助手。
    for (const productMode of ['assistant'] as const) {
      const button = document.createElement('button');
      const copy = document.createElement('span');
      const label = document.createElement('strong');
      const description = document.createElement('span');
      const modeIcon = createIcon(
        productMode === 'assistant' ? 'icon-agent' : 'icon-task',
        { className: 'mw-product-mode-menu__icon' },
      );
      const check = createIcon('icon-check', {
        className: 'mw-product-mode-menu__check',
      });
      button.type = 'button';
      button.className = 'mw-product-mode-menu__item';
      button.dataset.productModeOption = productMode;
      button.setAttribute('role', 'menuitemradio');
      button.setAttribute('aria-checked', String(productMode === current));
      button.setAttribute('aria-current', String(productMode === current));
      copy.className = 'mw-product-mode-menu__copy';
      label.textContent = PRODUCT_COPY[productMode].label;
      description.textContent = PRODUCT_COPY[productMode].description;
      check.toggleAttribute('hidden', productMode !== current);
      copy.append(label, description);
      button.append(modeIcon, copy, check);
      menu.append(button);
    }
    menu.addEventListener('click', (event) => {
      const target =
        event.target instanceof Element
          ? event.target.closest<HTMLButtonElement>('[data-product-mode-option]')
          : null;
      const productMode = target?.dataset.productModeOption;
      if (productMode === 'assistant') switchProductMode(productMode);
    });
    menu.addEventListener('keydown', (event) => {
      const items = [
        ...menu.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]'),
      ];
      const currentIndex = items.indexOf(document.activeElement as HTMLButtonElement);
      let next: HTMLButtonElement | undefined;
      if (event.key === 'Home') next = items[0];
      else if (event.key === 'End') next = items.at(-1);
      else if (event.key === 'ArrowDown') next = items[(currentIndex + 1) % items.length];
      else if (event.key === 'ArrowUp') {
        next = items[(currentIndex - 1 + items.length) % items.length];
      }
      if (!next) return;
      event.preventDefault();
      next.focus();
    });
    productMenu = openPopover({
      anchor: productTrigger,
      label: '切换助手模式',
      content: menu,
      onClose: () => {
        productMenu = null;
      },
    });
    productMenu.element.classList.add('mw-product-mode-popover');
    productMenu.element.setAttribute('role', 'presentation');
    productTrigger.setAttribute('aria-haspopup', 'menu');
  };

  const renderNavigation = (): void => {
    const focusedLocation =
      document.activeElement instanceof HTMLElement &&
      navigationList.contains(document.activeElement)
        ? document.activeElement.dataset.shellLocation
        : undefined;
    const state = productModeStore.get();
    const modeView = state.views[state.productMode];
    const items = resolveShellNavigation(state.productMode, features);
    navigationList.replaceChildren();
    for (const item of items) {
      const button = document.createElement('button');
      const label = document.createElement('span');
      button.type = 'button';
      button.className = 'mw-shell-nav-item';
      button.dataset.shellLocation = item.id;
      button.dataset.featureState = item.featureState;
      button.disabled = item.featureState === 'unavailable';
      button.title =
        item.featureState === 'unavailable' ? `${item.label}（暂不可用）` : item.label;
      if (item.id === modeView.lastPosition) button.setAttribute('aria-current', 'page');
      label.className = 'mw-shell-nav-item__label';
      label.textContent = item.label;
      button.append(createIcon(item.icon, { className: 'mw-shell-nav-item__icon' }), label);
      if (item.featureState === 'unavailable') {
        const availability = document.createElement('span');
        availability.className = 'mw-shell-nav-item__availability';
        availability.textContent = '暂不可用';
        button.append(availability);
      }
      navigationList.append(button);
    }
    if (focusedLocation) {
      navigationList
        .querySelector<HTMLButtonElement>(`[data-shell-location="${focusedLocation}"]`)
        ?.focus();
    }
    const active = items.find((item) => item.id === modeView.lastPosition) ?? items[0];
    contextTitle.textContent = active?.label ?? PRODUCT_COPY[state.productMode].label;
  };

  const sync = (): void => {
    const state = productModeStore.get();
    const modeView = state.views[state.productMode];
    const hasContext = state.productMode === 'work'
      ? modeView.lastPosition === 'workbench' || modeView.lastPosition === 'knowledge'
      : modeView.lastPosition === 'chat';
    const workContextCollapsed = state.productMode === 'work' && modeView.navigationCollapsed;
    element.dataset.productMode = state.productMode;
    element.dataset.navigationCollapsed = String(modeView.navigationCollapsed);
    template.element.dataset.hasContext = String(hasContext && !workContextCollapsed);
    if (template.slots.context) template.slots.context.hidden = !hasContext || workContextCollapsed;
    productLabel.textContent = PRODUCT_COPY[state.productMode].label;
    productTrigger.setAttribute('aria-label', PRODUCT_COPY[state.productMode].label);
    const assistantChat = state.productMode === 'assistant' && hasContext;
    const syncCollapseCommand = (button: HTMLButtonElement, noun: string): void => {
      const label = modeView.navigationCollapsed ? `展开${noun}` : `收起${noun}`;
      button.title = label;
      button.setAttribute('aria-label', label);
      button.setAttribute('aria-expanded', String(!modeView.navigationCollapsed));
      const text = button.querySelector<HTMLElement>('.mw-app-command__label');
      if (text) text.textContent = label;
    };
    contextCollapseCommand.hidden = state.productMode !== 'work';
    restoreContextCommand.hidden = !workContextCollapsed;
    historyActions.hidden = !assistantChat;
    historyCollapseCommand.hidden = !assistantChat;
    syncCollapseCommand(contextCollapseCommand, '上下文');
    syncCollapseCommand(historyCollapseCommand, '历史');
    newChatCommand.hidden = !(assistantChat && modeView.navigationCollapsed);
    renderNavigation();
  };

  const handleClick = (event: MouseEvent): void => {
    const target = event.target instanceof Element ? event.target : null;
    const locationButton = target?.closest<HTMLButtonElement>('[data-shell-location]');
    if (locationButton) {
      const location = locationButton.dataset.shellLocation as ShellLocation | undefined;
      const featureState = locationButton.dataset.featureState as FeatureState | undefined;
      if (!location || featureState !== 'available') return;
      const accepted = options.onNavigate?.(location, productModeStore.get().productMode);
      if (accepted === false) return;
      updateProductModeView({ lastPosition: location }, storage);
      return;
    }
    const commandButton = target?.closest<HTMLButtonElement>('[data-shell-command]');
    if (!commandButton) return;
    const command = commandButton.dataset.shellCommand;
    if (command === 'collapse') {
      const state = productModeStore.get();
      updateProductModeView(
        { navigationCollapsed: !state.views[state.productMode].navigationCollapsed },
        storage,
      );
      return;
    }
    const callback = options.commands?.[command as keyof ApplicationShellCommands];
    if (callback) void callback();
    if (command === 'newChat') {
      updateProductModeView({ lastPosition: 'chat' }, storage);
    }
  };

  element.addEventListener('click', handleClick);
  const unsubscribe = productModeStore.subscribe(sync);
  sync();

  return {
    element,
    slots: {
      context: template.slots.context as HTMLElement,
      contextContent,
      historyActions,
      page: template.slots.main,
    },
    setFeatures(nextFeatures) {
      const merged = { ...features, ...nextFeatures };
      if (nextFeatures.work) merged.work = { ...features.work, ...nextFeatures.work };
      features = merged;
      sync();
    },
    setProductMode: switchProductMode,
    dispose() {
      if (disposed) return;
      disposed = true;
      closeProductMenu();
      unsubscribe();
      element.removeEventListener('click', handleClick);
    },
  };
}
