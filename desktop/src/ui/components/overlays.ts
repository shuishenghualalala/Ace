import {
  createButton,
  createIconButton,
  createStatus,
  type ButtonVariant,
  type Tone,
} from './controls';
import { setRuntimeStyle } from './runtime-style';

export interface OverlayHandle<T extends HTMLElement = HTMLElement> {
  element: T;
  close(): void;
  dispose(): void;
}

interface LifecycleOptions {
  root: HTMLElement;
  panel: HTMLElement;
  trigger?: HTMLElement | undefined;
  initialFocus?: HTMLElement | undefined;
  modal: boolean;
  dismissible: boolean;
  closeOnOutside: boolean;
  removeOnClose?: boolean;
  beforeRemove?: (() => void) | undefined;
  onClose?: (() => void) | undefined;
}

const overlayStack: object[] = [];

function overlayMountHost(): HTMLElement {
  return document.getElementById('renderer-overlay-host') ?? document.body;
}

function focusableElements(root: HTMLElement): HTMLElement[] {
  return [
    ...root.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), ' +
        'textarea:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])',
    ),
  ].filter((element) => !element.hasAttribute('hidden'));
}

/**
 * Owns the shared overlay close, focus-loop and focus-restore lifecycle.
 */
function mountOverlay(options: LifecycleOptions): OverlayHandle {
  const token = {};
  const previousFocus =
    options.trigger ??
    (document.activeElement instanceof HTMLElement ? document.activeElement : undefined);
  let closed = false;

  const isTop = (): boolean => overlayStack.at(-1) === token;
  const close = (): void => {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', handleKeydown, true);
    document.removeEventListener('pointerdown', handlePointerdown, true);
    const index = overlayStack.lastIndexOf(token);
    if (index >= 0) overlayStack.splice(index, 1);
    options.beforeRemove?.();
    if (options.removeOnClose ?? true) options.root.remove();
    if (previousFocus?.isConnected) previousFocus.focus();
    options.onClose?.();
  };
  const handleKeydown = (event: KeyboardEvent): void => {
    if (!isTop()) return;
    if (event.key === 'Escape' && options.dismissible) {
      event.preventDefault();
      event.stopPropagation();
      close();
      return;
    }
    if (event.key !== 'Tab' || !options.modal) return;
    const focusable = focusableElements(options.panel);
    if (focusable.length === 0) {
      event.preventDefault();
      options.panel.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  };
  const handlePointerdown = (event: PointerEvent): void => {
    if (!isTop() || !options.closeOnOutside || !options.dismissible) return;
    const target = event.target;
    if (!(target instanceof Node)) return;
    if (options.panel.contains(target) || options.trigger?.contains(target)) return;
    close();
  };

  overlayMountHost().append(options.root);
  overlayStack.push(token);
  document.addEventListener('keydown', handleKeydown, true);
  document.addEventListener('pointerdown', handlePointerdown, true);
  const initial = options.initialFocus ?? focusableElements(options.panel)[0] ?? options.panel;
  if (initial === options.panel && !options.panel.hasAttribute('tabindex')) {
    options.panel.tabIndex = -1;
  }
  initial.focus();

  return { element: options.root, close, dispose: close };
}

export interface ExistingModalOptions {
  root: HTMLElement;
  panel: HTMLElement;
  trigger?: HTMLElement | undefined;
  initialFocus?: HTMLElement | undefined;
  dismissible?: boolean;
  onClose?: (() => void) | undefined;
}

/**
 * Gives an existing modal the same stack, focus and dismissal lifecycle as a
 * Renderer-owned dialog without changing its internal behavior contract.
 */
export function activateExistingModal(
  options: ExistingModalOptions,
): OverlayHandle {
  const dismissible = options.dismissible ?? true;
  options.root.hidden = false;
  options.root.classList.add('show');
  options.root.setAttribute('aria-modal', 'true');
  options.panel.setAttribute('role', 'dialog');
  options.panel.setAttribute('aria-modal', 'true');
  return mountOverlay({
    root: options.root,
    panel: options.panel,
    trigger: options.trigger,
    initialFocus: options.initialFocus,
    modal: true,
    dismissible,
    closeOnOutside: true,
    removeOnClose: false,
    beforeRemove: () => {
      options.root.classList.remove('show');
      options.root.hidden = true;
    },
    onClose: options.onClose,
  });
}

function positionFloating(
  element: HTMLElement,
  anchor: HTMLElement,
  align: 'start' | 'end' = 'start',
): void {
  const gap = 6;
  const edge = 8;
  const anchorRect = anchor.getBoundingClientRect();
  const width = element.offsetWidth || 240;
  const height = element.offsetHeight || 180;
  let left = align === 'end' ? anchorRect.right - width : anchorRect.left;
  let top = anchorRect.bottom + gap;
  left = Math.max(edge, Math.min(left, window.innerWidth - width - edge));
  if (top + height > window.innerHeight - edge) {
    top = Math.max(edge, anchorRect.top - height - gap);
  }
  setRuntimeStyle(element, 'left', `${left}px`);
  setRuntimeStyle(element, 'top', `${top}px`);
}

export interface MenuItem {
  id: string;
  label: string;
  disabled?: boolean;
  danger?: boolean;
  onSelect: () => void | Promise<void>;
}

export interface MenuOptions {
  anchor: HTMLElement;
  label: string;
  items: MenuItem[];
  align?: 'start' | 'end';
  onClose?: () => void;
}

export function openMenu(options: MenuOptions): OverlayHandle<HTMLDivElement> {
  const menu = document.createElement('div');
  menu.className = 'mw-floating mw-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', options.label);
  options.anchor.setAttribute('aria-haspopup', 'menu');
  options.anchor.setAttribute('aria-expanded', 'true');

  for (const item of options.items) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'mw-menu__item';
    button.dataset.menuItem = item.id;
    button.setAttribute('role', 'menuitem');
    button.disabled = item.disabled ?? false;
    if (item.danger) button.dataset.tone = 'danger';
    button.textContent = item.label;
    menu.append(button);
  }

  let handle: OverlayHandle<HTMLDivElement>;
  const enabledItems = (): HTMLButtonElement[] => [
    ...menu.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'),
  ];
  const handleClick = (event: MouseEvent): void => {
    const target =
      event.target instanceof Element
        ? event.target.closest<HTMLButtonElement>('[role="menuitem"]')
        : null;
    if (!target || target.disabled) return;
    const item = options.items.find((candidate) => candidate.id === target.dataset.menuItem);
    handle.close();
    if (item) void item.onSelect();
  };
  const handleKeydown = (event: KeyboardEvent): void => {
    const items = enabledItems();
    const current = document.activeElement as HTMLButtonElement | null;
    const index = current ? items.indexOf(current) : -1;
    let next: HTMLButtonElement | undefined;
    if (event.key === 'Home') next = items[0];
    if (event.key === 'End') next = items.at(-1);
    if (event.key === 'ArrowDown') next = items[(index + 1) % items.length];
    if (event.key === 'ArrowUp') next = items[(index - 1 + items.length) % items.length];
    if (!next) return;
    event.preventDefault();
    next.focus();
  };
  menu.addEventListener('click', handleClick);
  menu.addEventListener('keydown', handleKeydown);
  handle = mountOverlay({
    root: menu,
    panel: menu,
    trigger: options.anchor,
    initialFocus: enabledItems()[0],
    modal: false,
    dismissible: true,
    closeOnOutside: true,
    beforeRemove: () => {
      menu.removeEventListener('click', handleClick);
      menu.removeEventListener('keydown', handleKeydown);
      options.anchor.setAttribute('aria-expanded', 'false');
    },
    onClose: options.onClose,
  }) as OverlayHandle<HTMLDivElement>;
  positionFloating(menu, options.anchor, options.align);
  return handle;
}

export interface PopoverOptions {
  anchor: HTMLElement;
  label: string;
  content: Node;
  align?: 'start' | 'end';
  dismissible?: boolean;
  onClose?: () => void;
}

export function openPopover(options: PopoverOptions): OverlayHandle<HTMLDivElement> {
  const popover = document.createElement('div');
  popover.className = 'mw-floating mw-popover';
  popover.setAttribute('role', 'dialog');
  popover.setAttribute('aria-label', options.label);
  popover.append(options.content);
  options.anchor.setAttribute('aria-haspopup', 'dialog');
  options.anchor.setAttribute('aria-expanded', 'true');
  const handle = mountOverlay({
    root: popover,
    panel: popover,
    trigger: options.anchor,
    initialFocus: focusableElements(popover)[0],
    modal: false,
    dismissible: options.dismissible ?? true,
    closeOnOutside: true,
    beforeRemove: () => options.anchor.setAttribute('aria-expanded', 'false'),
    onClose: options.onClose,
  }) as OverlayHandle<HTMLDivElement>;
  positionFloating(popover, options.anchor, options.align);
  return handle;
}

export interface DialogAction {
  label: string;
  variant?: ButtonVariant;
  disabled?: boolean;
  onPress?: () => void;
}

export interface DialogOptions {
  trigger?: HTMLElement | undefined;
  title: string;
  content: Node | string;
  actions?: DialogAction[];
  dismissible?: boolean;
  onClose?: (() => void) | undefined;
}

let dialogId = 0;

function openModal(
  kind: 'dialog' | 'drawer',
  options: DialogOptions,
): OverlayHandle<HTMLDivElement> {
  const layer = document.createElement('div');
  const panel = document.createElement('section');
  const header = document.createElement('header');
  const title = document.createElement('h2');
  const body = document.createElement('div');
  const footer = document.createElement('footer');
  const controls: Array<{ dispose(): void }> = [];
  const titleId = `mw-${kind}-title-${++dialogId}`;
  const dismissible = options.dismissible ?? true;
  let handle: OverlayHandle<HTMLDivElement>;

  layer.className =
    kind === 'drawer' ? 'mw-overlay-layer mw-overlay-layer--drawer' : 'mw-overlay-layer';
  panel.className = kind === 'drawer' ? 'mw-drawer' : 'mw-dialog';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  panel.setAttribute('aria-labelledby', titleId);
  header.className = `mw-${kind}__header`;
  title.id = titleId;
  title.textContent = options.title;
  body.className = `mw-${kind}__body`;
  if (typeof options.content === 'string') body.textContent = options.content;
  else body.append(options.content);
  footer.className = `mw-${kind}__footer`;
  header.append(title);

  if (dismissible) {
    const closeButton = createIconButton({
      icon: 'icon-close',
      label: '关闭',
      variant: 'ghost',
      onPress: () => handle.close(),
    });
    closeButton.element.classList.add(`mw-${kind}__close`);
    header.append(closeButton.element);
    controls.push(closeButton);
  }

  for (const action of options.actions ?? []) {
    const control = createButton({
      label: action.label,
      variant: action.variant ?? 'secondary',
      disabled: action.disabled ?? false,
      onPress: () => {
        handle.close();
        action.onPress?.();
      },
    });
    footer.append(control.element);
    controls.push(control);
  }

  panel.append(header, body);
  if (footer.childElementCount > 0) panel.append(footer);
  layer.append(panel);
  const contentFocus = focusableElements(body)[0];
  const actionFocus = footer.querySelector<HTMLElement>('.mw-button:not(:disabled)') ?? undefined;
  handle = mountOverlay({
    root: layer,
    panel,
    trigger: options.trigger,
    initialFocus: contentFocus ?? actionFocus,
    modal: true,
    dismissible,
    closeOnOutside: true,
    beforeRemove: () => {
      for (const control of controls) control.dispose();
    },
    onClose: options.onClose,
  }) as OverlayHandle<HTMLDivElement>;
  return handle;
}

export function openDialog(options: DialogOptions): OverlayHandle<HTMLDivElement> {
  return openModal('dialog', options);
}

export function openDrawer(options: DialogOptions): OverlayHandle<HTMLDivElement> {
  return openModal('drawer', options);
}

export interface ConfirmDialogOptions {
  trigger?: HTMLElement | undefined;
  title: string;
  object: string;
  consequence: string;
  confirmLabel: string;
  cancelLabel?: string;
  onConfirm: () => void;
  onClose?: (() => void) | undefined;
}

export function openConfirmDialog(
  options: ConfirmDialogOptions,
): OverlayHandle<HTMLDivElement> {
  if (!options.object.trim() || !options.consequence.trim()) {
    throw new Error('Destructive confirmation requires an object and consequence');
  }
  const content = document.createElement('div');
  const object = document.createElement('strong');
  const consequence = document.createElement('p');
  content.className = 'mw-dialog__confirmation';
  object.textContent = options.object;
  consequence.textContent = options.consequence;
  content.append(object, consequence);
  return openDialog({
    trigger: options.trigger,
    title: options.title,
    content,
    actions: [
      { label: options.cancelLabel ?? '取消', variant: 'ghost' },
      { label: options.confirmLabel, variant: 'danger', onPress: options.onConfirm },
    ],
    onClose: options.onClose,
  });
}

export interface ToastOptions {
  message: string;
  tone?: Tone;
  duration?: number;
}

let toastHost: HTMLDivElement | null = null;

function ensureToastHost(): HTMLDivElement {
  if (toastHost?.isConnected) return toastHost;
  toastHost = document.createElement('div');
  toastHost.className = 'mw-toast-host';
  toastHost.setAttribute('aria-live', 'polite');
  overlayMountHost().append(toastHost);
  return toastHost;
}

export function showToast(options: ToastOptions): OverlayHandle<HTMLDivElement> {
  const host = ensureToastHost();
  const toast = document.createElement('div');
  const tone = options.tone ?? 'neutral';
  const status = createStatus({ label: options.message, tone });
  let closed = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  toast.className = 'mw-toast';
  toast.dataset.tone = tone;
  toast.setAttribute('role', tone === 'danger' ? 'alert' : 'status');
  status.removeAttribute('role');
  const closeButton = createIconButton({
    icon: 'icon-close',
    label: '关闭通知',
    variant: 'ghost',
    size: 'small',
    onPress: close,
  });
  toast.append(status, closeButton.element);
  host.append(toast);

  function close(): void {
    if (closed) return;
    closed = true;
    if (timer) clearTimeout(timer);
    closeButton.dispose();
    toast.remove();
    if (host.childElementCount === 0) {
      host.remove();
      if (toastHost === host) toastHost = null;
    }
  }

  const duration = options.duration ?? 4000;
  if (duration > 0) timer = setTimeout(close, duration);
  return { element: toast, close, dispose: close };
}
