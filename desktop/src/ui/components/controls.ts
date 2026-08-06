import { createIcon, type IconId } from './icon';

export type ButtonVariant = 'primary' | 'secondary' | 'outline' | 'ghost' | 'danger';
export type ControlSize = 'small' | 'default' | 'large';

export interface ButtonOptions {
  label: string;
  icon?: IconId;
  variant?: ButtonVariant;
  size?: ControlSize;
  disabled?: boolean;
  loading?: boolean;
  onPress?: (event: MouseEvent) => void;
}

export interface ButtonControl {
  element: HTMLButtonElement;
  setDisabled(disabled: boolean): void;
  setLoading(loading: boolean): void;
  dispose(): void;
}

/**
 * Creates a shared text button with explicit disabled and loading state priority.
 */
export function createButton(options: ButtonOptions): ButtonControl {
  const button = document.createElement('button');
  const leading = document.createElement('span');
  const label = document.createElement('span');
  let disabled = options.disabled ?? false;
  let loading = options.loading ?? false;

  button.type = 'button';
  button.className = [
    'mw-button',
    `mw-button--${options.variant ?? 'secondary'}`,
    `mw-button--${options.size ?? 'default'}`,
  ].join(' ');
  button.dataset.hasIcon = String(Boolean(options.icon));
  leading.className = 'mw-button__leading';
  if (options.icon) leading.append(createIcon(options.icon));
  leading.append(createIcon('loading-frame', { className: 'mw-button__spinner' }));
  label.className = 'mw-button__label';
  label.textContent = options.label;
  button.append(leading, label);

  const syncState = (): void => {
    button.disabled = disabled || loading;
    button.dataset.loading = String(loading);
    if (loading) button.setAttribute('aria-busy', 'true');
    else button.removeAttribute('aria-busy');
  };
  const handlePress = (event: MouseEvent): void => {
    if (!disabled && !loading) options.onPress?.(event);
  };

  button.addEventListener('click', handlePress);
  syncState();
  return {
    element: button,
    setDisabled(nextDisabled) {
      disabled = nextDisabled;
      syncState();
    },
    setLoading(nextLoading) {
      loading = nextLoading;
      syncState();
    },
    dispose() {
      button.removeEventListener('click', handlePress);
    },
  };
}

export interface IconButtonOptions extends Omit<ButtonOptions, 'icon'> {
  icon: IconId;
  tooltip?: string;
}

/**
 * Creates a square icon button. Its required label is both its accessible name
 * and default tooltip.
 */
export function createIconButton(options: IconButtonOptions): ButtonControl {
  const { tooltip, ...buttonOptions } = options;
  const control = createButton({
    ...buttonOptions,
    variant: options.variant ?? 'ghost',
  });
  control.element.classList.add('mw-button--icon');
  control.element.setAttribute('aria-label', options.label);
  control.element.title = tooltip ?? options.label;
  control.element.querySelector('.mw-button__label')?.classList.add('mw-sr-only');
  return control;
}

export type FieldKind =
  | 'text'
  | 'search'
  | 'email'
  | 'password'
  | 'date'
  | 'time'
  | 'select'
  | 'textarea';

export interface FieldOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface FieldOptions {
  kind: FieldKind;
  label: string;
  name: string;
  value?: string;
  placeholder?: string;
  helper?: string;
  error?: string;
  options?: FieldOption[];
  required?: boolean;
  disabled?: boolean;
  large?: boolean;
  onInput?: (value: string) => void;
}

export interface FieldControl {
  element: HTMLDivElement;
  control: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
  setDisabled(disabled: boolean): void;
  setError(error: string | null): void;
  dispose(): void;
}

let fieldId = 0;

/**
 * Creates a labelled native form control with a stable helper/error slot.
 */
export function createField(options: FieldOptions): FieldControl {
  const element = document.createElement('div');
  const label = document.createElement('label');
  const message = document.createElement('div');
  const id = `mw-field-${++fieldId}`;
  let control: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;

  if (options.kind === 'select') {
    const select = document.createElement('select');
    for (const item of options.options ?? []) {
      const option = document.createElement('option');
      option.value = item.value;
      option.textContent = item.label;
      option.disabled = item.disabled ?? false;
      select.append(option);
    }
    control = select;
  } else if (options.kind === 'textarea') {
    control = document.createElement('textarea');
  } else {
    const input = document.createElement('input');
    input.type = options.kind;
    control = input;
  }

  element.className = 'mw-field';
  label.className = 'mw-field__label';
  label.htmlFor = id;
  label.textContent = options.label;
  control.id = id;
  control.name = options.name;
  control.className = options.large
    ? 'mw-field__control mw-field__control--large'
    : 'mw-field__control';
  control.value = options.value ?? '';
  control.disabled = options.disabled ?? false;
  control.required = options.required ?? false;
  if (options.placeholder && control instanceof HTMLInputElement) {
    control.placeholder = options.placeholder;
  }
  if (options.placeholder && control instanceof HTMLTextAreaElement) {
    control.placeholder = options.placeholder;
  }
  message.id = `${id}-message`;
  message.className = 'mw-field__message';
  control.setAttribute('aria-describedby', message.id);
  element.append(label, control, message);

  const setError = (error: string | null): void => {
    message.replaceChildren();
    if (error) {
      control.setAttribute('aria-invalid', 'true');
      message.dataset.tone = 'danger';
      message.setAttribute('role', 'alert');
      message.append(
        createIcon('icon-error', { className: 'mw-field__message-icon' }),
        document.createTextNode(error),
      );
      return;
    }
    control.removeAttribute('aria-invalid');
    message.dataset.tone = 'neutral';
    message.removeAttribute('role');
    message.textContent = options.helper ?? '';
  };
  const handleInput = (): void => options.onInput?.(control.value);

  control.addEventListener('input', handleInput);
  setError(options.error ?? null);
  return {
    element,
    control,
    setDisabled(nextDisabled) {
      control.disabled = nextDisabled;
    },
    setError,
    dispose() {
      control.removeEventListener('input', handleInput);
    },
  };
}

export interface TabItem {
  id: string;
  label: string;
  count?: number;
  disabled?: boolean;
}

export interface TabsOptions {
  label: string;
  value: string;
  items: TabItem[];
  onChange?: (id: string) => void;
}

export interface TabsControl {
  element: HTMLDivElement;
  setValue(id: string): void;
  dispose(): void;
}

/**
 * Creates an ARIA tab row with automatic activation and native button focus.
 */
export function createTabs(options: TabsOptions): TabsControl {
  const element = document.createElement('div');
  element.className = 'mw-tabs';
  element.setAttribute('role', 'tablist');
  element.setAttribute('aria-label', options.label);

  for (const item of options.items) {
    const tab = document.createElement('button');
    tab.type = 'button';
    tab.className = 'mw-tab';
    tab.dataset.tabId = item.id;
    tab.setAttribute('role', 'tab');
    tab.disabled = item.disabled ?? false;
    tab.append(document.createTextNode(item.label));
    if (item.count !== undefined) {
      tab.append(createBadge({ label: String(item.count), tone: 'neutral', compact: true }));
    }
    element.append(tab);
  }

  const buttons = (): HTMLButtonElement[] => [
    ...element.querySelectorAll<HTMLButtonElement>('[role="tab"]'),
  ];
  const activate = (id: string, emit: boolean, focus: boolean): void => {
    const target = buttons().find((button) => button.dataset.tabId === id && !button.disabled);
    if (!target) return;
    for (const button of buttons()) {
      const selected = button === target;
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
    }
    element.dataset.value = id;
    if (focus) target.focus();
    if (emit) options.onChange?.(id);
  };
  const handleClick = (event: MouseEvent): void => {
    const target = (event.target as Element).closest<HTMLButtonElement>('[role="tab"]');
    if (target && element.contains(target)) activate(target.dataset.tabId ?? '', true, false);
  };
  const handleKeydown = (event: KeyboardEvent): void => {
    const target = (event.target as Element).closest<HTMLButtonElement>('[role="tab"]');
    if (!target || !element.contains(target)) return;
    const enabled = buttons().filter((button) => !button.disabled);
    const index = enabled.indexOf(target);
    let next: HTMLButtonElement | undefined;
    if (event.key === 'Home') next = enabled[0];
    if (event.key === 'End') next = enabled.at(-1);
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      next = enabled[(index + 1) % enabled.length];
    }
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      next = enabled[(index - 1 + enabled.length) % enabled.length];
    }
    if (!next) return;
    event.preventDefault();
    activate(next.dataset.tabId ?? '', true, true);
  };

  element.addEventListener('click', handleClick);
  element.addEventListener('keydown', handleKeydown);
  for (const button of buttons()) {
    button.setAttribute('aria-selected', 'false');
    button.tabIndex = -1;
  }
  const initial =
    buttons().find((button) => button.dataset.tabId === options.value && !button.disabled) ??
    buttons().find((button) => !button.disabled);
  if (initial) activate(initial.dataset.tabId ?? '', false, false);
  return {
    element,
    setValue(id) {
      activate(id, false, false);
    },
    dispose() {
      element.removeEventListener('click', handleClick);
      element.removeEventListener('keydown', handleKeydown);
    },
  };
}

export type Tone = 'neutral' | 'success' | 'warning' | 'danger' | 'info';

export interface BadgeOptions {
  label: string;
  tone?: Tone;
  compact?: boolean;
}

export function createBadge(options: BadgeOptions): HTMLSpanElement {
  const badge = document.createElement('span');
  badge.className = options.compact ? 'mw-badge mw-badge--compact' : 'mw-badge';
  badge.dataset.tone = options.tone ?? 'neutral';
  badge.textContent = options.label;
  return badge;
}

export type StatusTone = Tone | 'running' | 'waiting';

const STATUS_ICONS: Record<StatusTone, IconId> = {
  neutral: 'icon-check',
  success: 'status-complete',
  warning: 'icon-warning',
  danger: 'process-error',
  info: 'process-clock',
  running: 'status-running',
  waiting: 'status-waiting',
};

export function createStatus(options: { label: string; tone: StatusTone }): HTMLSpanElement {
  const status = document.createElement('span');
  status.className = 'mw-status';
  status.dataset.tone = options.tone;
  status.setAttribute('role', 'status');
  status.append(
    createIcon(STATUS_ICONS[options.tone], { className: 'mw-status__icon' }),
    document.createTextNode(options.label),
  );
  return status;
}

export interface ListItem {
  id: string;
  label: string;
  description?: string;
  icon?: IconId;
  disabled?: boolean;
}

export interface ListOptions {
  label: string;
  items: ListItem[];
  selectedId?: string | null;
  onSelect?: (id: string) => void;
}

export interface ListControl {
  element: HTMLUListElement;
  setSelected(id: string | null): void;
  dispose(): void;
}

/**
 * Creates a single-selection action list using native buttons.
 */
export function createList(options: ListOptions): ListControl {
  const element = document.createElement('ul');
  element.className = 'mw-list';
  element.setAttribute('aria-label', options.label);

  for (const item of options.items) {
    const row = document.createElement('li');
    const button = document.createElement('button');
    const copy = document.createElement('span');
    const label = document.createElement('span');
    button.type = 'button';
    button.className = 'mw-list__item';
    button.dataset.listItem = item.id;
    button.disabled = item.disabled ?? false;
    if (item.icon) button.append(createIcon(item.icon, { className: 'mw-list__icon' }));
    copy.className = 'mw-list__copy';
    label.className = 'mw-list__label';
    label.textContent = item.label;
    copy.append(label);
    if (item.description) {
      const description = document.createElement('span');
      description.className = 'mw-list__description';
      description.textContent = item.description;
      copy.append(description);
    }
    button.append(copy);
    row.append(button);
    element.append(row);
  }

  const setSelected = (id: string | null): void => {
    for (const button of element.querySelectorAll<HTMLButtonElement>('.mw-list__item')) {
      const selected = button.dataset.listItem === id;
      button.classList.toggle('is-selected', selected);
      if (selected) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
    }
  };
  const handleClick = (event: MouseEvent): void => {
    const target = (event.target as Element).closest<HTMLButtonElement>('.mw-list__item');
    if (!target || !element.contains(target) || target.disabled) return;
    const id = target.dataset.listItem;
    if (!id) return;
    setSelected(id);
    options.onSelect?.(id);
  };

  element.addEventListener('click', handleClick);
  setSelected(options.selectedId ?? null);
  return {
    element,
    setSelected,
    dispose() {
      element.removeEventListener('click', handleClick);
    },
  };
}
