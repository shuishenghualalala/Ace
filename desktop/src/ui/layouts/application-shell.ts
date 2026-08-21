import { createIcon, MONOCHROME_ICON_CLASS } from '../components/icon';
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

const PRODUCT_LABEL: Record<ProductMode, string> = {
  assistant: 'Crew',
  work: 'Crew 办公助手',
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
  let disposed = false;

  restoreProductMode(storage);

  const element = document.createElement('div');
  const titlebar = document.createElement('header');
  const productBrand = document.createElement('div');
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
  productBrand.className = 'mw-sidebar-brand';
  productBrand.setAttribute('role', 'img');
  productBrand.setAttribute('aria-label', 'Crew');
  productIcon.className = 'mw-sidebar-brand__icon';
  productIcon.src = options.productIconUrl ?? './icon.png';
  productIcon.alt = '';
  productLabel.className = 'mw-sidebar-brand__label';
  productBrand.append(productIcon, productLabel);

  windowCommands.className = 'mw-window-commands';
  windowCommands.append(
    createWindowButton('minimize', '最小化', '−'),
    createWindowButton('maximize', '最大化或还原', '□'),
    createWindowButton('close', '关闭', '×'),
  );
  titlebar.append(windowCommands);

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
  // ponytail: 账户入口从对话页右上角迁到导航栏底部（参考 MobileMate）。
  // 点击由 login.ts 监听 [data-user-account-trigger] 派发 user:open-account，CSS 复用 .mw-app-account*。
  const accountSection = document.createElement('div');
  const accountButton = document.createElement('button');
  const accountAvatar = document.createElement('span');
  const accountDetails = document.createElement('span');
  const accountName = document.createElement('span');
  accountSection.id = 'user-section';
  accountSection.className = 'mw-app-account';
  accountButton.type = 'button';
  accountButton.className = 'mw-app-account__trigger';
  accountButton.dataset.userAccountTrigger = '';
  accountButton.setAttribute('aria-label', '账户');
  accountAvatar.className = 'mw-app-account__avatar';
  accountAvatar.dataset.userAvatar = '';
  accountAvatar.textContent = '我';
  accountDetails.className = 'mw-app-account__details';
  accountName.className = 'mw-app-account__name';
  accountName.dataset.userName = '';
  accountDetails.append(accountName);
  accountButton.append(accountAvatar, accountDetails);
  accountSection.append(accountButton);
  updateCommand.hidden = true;
  updatePercent.id = 'version-update-percent';
  updatePercent.className = 'mw-app-command__progress';
  updatePercent.hidden = true;
  updateCommand.append(updatePercent);
  navigationFooter.append(
    accountSection,
    updateCommand,
    settingsCommand,
  );
  navigation.append(productBrand, navigationList, navigationFooter);
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

  const switchProductMode = (productMode: ProductMode): void => {
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
      const unavailableTitle = item.id === 'security'
        ? '功能正在开发中，敬请期待'
        : `${item.label}（暂不可用）`;
      button.title = item.featureState === 'unavailable' ? unavailableTitle : item.label;
      if (item.id === modeView.lastPosition) button.setAttribute('aria-current', 'page');
      label.className = 'mw-shell-nav-item__label';
      label.textContent = item.label;
      button.append(createIcon(item.icon, {
        className: item.icon === 'icon-agent' || item.icon === 'icon-external-agent'
          ? `mw-shell-nav-item__icon ${MONOCHROME_ICON_CLASS}`
          : 'mw-shell-nav-item__icon',
      }), label);
      if (item.featureState === 'unavailable') {
        if (item.id !== 'security') {
          const availability = document.createElement('span');
          availability.className = 'mw-shell-nav-item__availability';
          availability.textContent = '暂不可用';
          button.append(availability);
        }
      }
      navigationList.append(button);
    }
    if (focusedLocation) {
      navigationList
        .querySelector<HTMLButtonElement>(`[data-shell-location="${focusedLocation}"]`)
        ?.focus();
    }
    const active = items.find((item) => item.id === modeView.lastPosition) ?? items[0];
    contextTitle.textContent = active?.label ?? PRODUCT_LABEL[state.productMode];
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
    productLabel.textContent = PRODUCT_LABEL[state.productMode];
    productBrand.setAttribute('aria-label', PRODUCT_LABEL[state.productMode]);
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
      unsubscribe();
      element.removeEventListener('click', handleClick);
    },
  };
}
