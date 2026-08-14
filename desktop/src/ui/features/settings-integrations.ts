import { createIcon, type IconId } from '../components/icon';
import { createStatus, type StatusTone } from '../components/controls';

export interface SettingsIntegrationAction {
  id: string;
  label: string;
  tone?: 'secondary' | 'danger';
  disabled?: boolean;
}

export interface SettingsIntegrationItem {
  id: string;
  title: string;
  description?: string;
  status: string;
  error?: string;
  tone: StatusTone;
  selectable: boolean;
  icon?: IconId;
  image?: { src: string; alt?: string };
  active?: boolean;
  actions?: SettingsIntegrationAction[];
}

export interface SettingsIntegrationSnapshot {
  state: 'loading' | 'ready' | 'empty' | 'error';
  message: string;
  items: SettingsIntegrationItem[];
}

export interface SettingsIntegrationView {
  element: HTMLElement;
  leading: HTMLElement;
  update(snapshot: SettingsIntegrationSnapshot): void;
}

export function createSettingsIntegrationView(options: {
  kind?: 'model' | 'channel' | 'mcp';
  title: string;
  description: string;
  primaryAction?: { label: string; icon?: IconId };
  onPrimaryAction?: () => void;
  onSelect(id: string): void;
  onAction(actionId: string, itemId: string): void;
}): SettingsIntegrationView {
  const element = document.createElement('section');
  const header = document.createElement('header');
  const heading = document.createElement('div');
  const title = document.createElement('h1');
  const description = document.createElement('p');
  const status = document.createElement('div');
  const leading = document.createElement('div');
  const list = document.createElement('div');
  let snapshot: SettingsIntegrationSnapshot = { state: 'loading', message: '', items: [] };

  element.className = 'settings-integrations';
  element.dataset.settingsIntegrations = '';
  if (options.kind) element.dataset.integrationKind = options.kind;
  header.className = 'settings-integrations__header';
  heading.className = 'settings-integrations__heading';
  title.className = 'settings-integrations__title';
  title.textContent = options.title;
  description.className = 'settings-integrations__description';
  description.textContent = options.description;
  heading.append(title, description);
  header.append(heading);
  if (options.primaryAction) {
    const primary = document.createElement('button');
    primary.type = 'button';
    primary.className = 'mw-button mw-button--primary mw-button--default';
    primary.dataset.integrationPrimary = '';
    primary.append(
      createIcon(options.primaryAction.icon ?? 'icon-plus', { size: 16 }),
      document.createTextNode(options.primaryAction.label),
    );
    header.append(primary);
  }
  status.className = 'settings-integrations__state';
  status.setAttribute('role', 'status');
  leading.className = 'settings-integrations__leading';
  list.className = 'settings-integrations__list';
  list.setAttribute('role', 'list');
  element.append(header, status, leading, list);

  const selectItem = (id: string): void => {
    const item = snapshot.items.find((candidate) => candidate.id === id);
    if (item?.selectable) options.onSelect(id);
  };
  const handleClick = (event: MouseEvent): void => {
    const target = event.target as Element;
    if (target.closest('[data-integration-primary]')) {
      options.onPrimaryAction?.();
      return;
    }
    const action = target.closest<HTMLButtonElement>('[data-integration-action]');
    if (action) {
      const row = action.closest<HTMLElement>('[data-integration-id]');
      if (row && !action.disabled) {
        options.onAction(action.dataset.integrationAction ?? '', row.dataset.integrationId ?? '');
      }
      return;
    }
    const select = target.closest<HTMLElement>('[data-integration-select]');
    const row = select?.closest<HTMLElement>('[data-integration-id]');
    if (row) selectItem(row.dataset.integrationId ?? '');
  };
  element.addEventListener('click', handleClick);

  return {
    element,
    leading,
    update(nextSnapshot) {
      snapshot = nextSnapshot;
      status.dataset.state = nextSnapshot.state;
      status.textContent = nextSnapshot.message;
      status.hidden = !nextSnapshot.message;
      list.replaceChildren();
      for (const item of nextSnapshot.items) {
        const row = document.createElement('article');
        const select = document.createElement(item.selectable ? 'button' : 'div');
        const symbol = document.createElement('span');
        const copy = document.createElement('span');
        const itemTitle = document.createElement('strong');
        const actions = document.createElement('span');
        row.className = 'settings-integrations__item';
        row.dataset.integrationId = item.id;
        row.dataset.active = String(Boolean(item.active));
        row.setAttribute('role', 'listitem');
        row.setAttribute('aria-disabled', String(!item.selectable));
        if (item.selectable) {
          (select as HTMLButtonElement).type = 'button';
          select.setAttribute('aria-label', `${item.title}，${item.status}`);
        }
        select.className = 'settings-integrations__select';
        select.dataset.integrationSelect = '';
        symbol.className = 'settings-integrations__symbol';
        if (item.image) {
          const image = document.createElement('img');
          image.src = item.image.src;
          image.alt = item.image.alt ?? '';
          symbol.append(image);
        } else {
          symbol.append(createIcon(item.icon ?? 'process-terminal', { size: 20 }));
        }
        copy.className = 'settings-integrations__copy';
        itemTitle.className = 'settings-integrations__item-title';
        itemTitle.textContent = item.title;
        copy.append(itemTitle);
        if (item.description) {
          const itemDescription = document.createElement('span');
          itemDescription.className = 'settings-integrations__item-description';
          itemDescription.textContent = item.description;
          copy.append(itemDescription);
        }
        copy.append(createStatus({ label: item.status, tone: item.tone }));
        if (item.error) {
          const error = document.createElement('span');
          error.className = 'settings-integrations__item-error';
          error.textContent = item.error;
          copy.append(error);
        }
        actions.className = 'settings-integrations__actions';
        for (const action of item.actions ?? []) {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = action.tone === 'danger'
            ? 'mw-button mw-button--danger mw-button--small'
            : 'mw-button mw-button--secondary mw-button--small';
          button.dataset.integrationAction = action.id;
          button.disabled = Boolean(action.disabled);
          button.textContent = action.label;
          actions.append(button);
        }
        select.append(symbol, copy);
        row.append(select, actions);
        list.append(row);
      }
    },
  };
}
