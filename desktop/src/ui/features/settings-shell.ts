import { createIcon, type IconId } from '../components/icon';

const SETTINGS_NAVIGATION: ReadonlyArray<{ id: string; label: string; icon: IconId }> = [
  { id: 'account', label: '账户', icon: 'icon-agent' },
  { id: 'general', label: '通用', icon: 'icon-settings' },
  { id: 'model', label: '模型', icon: 'process-thinking' },
  { id: 'channel', label: '渠道', icon: 'process-web' },
  { id: 'mcp', label: 'MCP', icon: 'process-terminal' },
  { id: 'work', label: '办公助手', icon: 'icon-task' },
  { id: 'sys-logs', label: '系统日志', icon: 'icon-file' },
  { id: 'sys-usage', label: '使用统计', icon: 'process-clock' },
  { id: 'library', label: '资源库', icon: 'icon-folder' },
  { id: 'data', label: '数据管理', icon: 'icon-task' },
  { id: 'about', label: '关于', icon: 'icon-file' },
];

export interface SettingsShell {
  element: HTMLElement;
  open(): void;
  close(): void;
  setActivePane(paneId: string): void;
  setPaneVisible(paneId: string, visible: boolean): void;
  dispose(): void;
}

export function createSettingsShell(options: {
  panes: HTMLElement[];
  onPaneChange(paneId: string): void;
  onClose(): void;
}): SettingsShell {
  const element = document.createElement('div');
  const dialog = document.createElement('section');
  const navigation = document.createElement('nav');
  const navigationTitle = document.createElement('h2');
  const content = document.createElement('main');
  const close = document.createElement('button');
  const paneIds = new Set(options.panes.map((pane) => pane.id.replace('settings-pane-', '')));

  element.id = 'settings-modal';
  element.className = 'modal-overlay mw-settings-overlay';
  element.dataset.template = 'settings';
  element.hidden = true;
  dialog.className = 'mw-settings-dialog';
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-labelledby', 'settings-dialog-title');
  navigation.id = 'settings-nav';
  navigation.className = 'mw-settings-dialog__navigation set-v2-nav';
  navigation.setAttribute('aria-label', '设置分类');
  navigationTitle.id = 'settings-dialog-title';
  navigationTitle.className = 'mw-settings-dialog__title';
  navigationTitle.textContent = '设置';
  navigation.append(navigationTitle);

  for (const item of SETTINGS_NAVIGATION.filter((candidate) => paneIds.has(candidate.id))) {
    const button = document.createElement('button');
    const label = document.createElement('span');
    button.type = 'button';
    button.className = 'mw-settings-dialog__nav-item set-v2-nav__item';
    button.dataset.settingsPane = item.id;
    label.textContent = item.label;
    button.append(createIcon(item.icon, { size: 20 }), label);
    navigation.append(button);
  }

  content.className = 'mw-settings-dialog__content';
  content.append(...options.panes);
  close.id = 'settings-modal-close';
  close.type = 'button';
  close.className = 'mw-settings-dialog__close';
  close.setAttribute('aria-label', '关闭设置');
  close.append(createIcon('icon-close', { size: 18 }));
  dialog.append(navigation, content, close);
  element.append(dialog);

  const setActivePane = (paneId: string): void => {
    const targetButton = navigation.querySelector<HTMLButtonElement>(
      `[data-settings-pane="${paneId}"]`,
    );
    if (targetButton?.hidden) return;
    for (const button of navigation.querySelectorAll<HTMLButtonElement>('[data-settings-pane]')) {
      const active = button.dataset.settingsPane === paneId;
      button.classList.toggle('is-active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    }
    for (const pane of options.panes) {
      pane.hidden = pane.id !== `settings-pane-${paneId}`;
    }
  };
  const setPaneVisible = (paneId: string, visible: boolean): void => {
    const button = navigation.querySelector<HTMLButtonElement>(
      `[data-settings-pane="${paneId}"]`,
    );
    const pane = options.panes.find((candidate) => candidate.id === `settings-pane-${paneId}`);
    if (!button || !pane) return;
    const wasActive = button.getAttribute('aria-current') === 'page';
    button.hidden = !visible;
    if (visible || !wasActive) {
      if (!visible) pane.hidden = true;
      return;
    }
    const fallback = navigation.querySelector<HTMLButtonElement>(
      '[data-settings-pane]:not([hidden])',
    );
    setActivePane(fallback?.dataset.settingsPane ?? '');
  };
  const handleNavigation = (event: MouseEvent): void => {
    const target = (event.target as Element).closest<HTMLButtonElement>('[data-settings-pane]');
    if (!target || !navigation.contains(target)) return;
    const paneId = target.dataset.settingsPane;
    if (!paneId) return;
    setActivePane(paneId);
    options.onPaneChange(paneId);
  };
  const handleOverlay = (event: MouseEvent): void => {
    if (event.target === element) options.onClose();
  };
  const handleKeydown = (event: KeyboardEvent): void => {
    if (event.key === 'Escape' && element.classList.contains('show')) options.onClose();
  };

  navigation.addEventListener('click', handleNavigation);
  element.addEventListener('click', handleOverlay);
  close.addEventListener('click', options.onClose);
  document.addEventListener('keydown', handleKeydown);
  setActivePane(options.panes.some((pane) => pane.id === 'settings-pane-account')
    ? 'account'
    : paneIds.values().next().value ?? '');

  return {
    element,
    open() {
      element.classList.add('show');
      element.hidden = false;
      close.focus();
    },
    close() {
      element.classList.remove('show');
      element.hidden = true;
    },
    setActivePane,
    setPaneVisible,
    dispose() {
      navigation.removeEventListener('click', handleNavigation);
      element.removeEventListener('click', handleOverlay);
      close.removeEventListener('click', options.onClose);
      document.removeEventListener('keydown', handleKeydown);
    },
  };
}
