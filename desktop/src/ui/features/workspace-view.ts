import type { Workspace } from '../backend-client';
import {
  createButton,
  createField,
  createIconButton,
  type ButtonControl,
  type FieldControl,
  type StatusTone,
} from '../components/controls';
import { createIcon, type IconId } from '../components/icon';
import { sessionStore } from '../stores/session-store';
import { workspaceStore } from '../stores/workspace-store';

type Availability =
  | 'default'
  | 'unbound'
  | 'checking'
  | 'available'
  | 'unavailable'
  | 'unknown'
  | 'error';

interface AvailabilityRecord {
  path: string;
  state: Availability;
}

export interface WorkspaceDirectoryInfo {
  exists: boolean;
  canonicalPath: string | null;
}

export interface WorkspaceViewOptions {
  getWorkspaces(): readonly Workspace[];
  getSessionCount(workspaceId: string): number;
  getLoadError(): string | null;
  reloadWorkspaces?: () => void | Promise<void>;
  directoryInfo?: (workspaceId: string) => Promise<WorkspaceDirectoryInfo>;
  createWorkspace(): void | string | Promise<void | string>;
  saveWorkspace(
    workspaceId: string,
    fields: { name: string; instructions: string },
  ): void | Promise<void>;
  relinkWorkspace(workspaceId: string): void | string | Promise<void | string>;
  setWorkspaceHidden(workspaceId: string, hidden: boolean): void | Promise<void>;
  deleteWorkspace(workspaceId: string): void | Promise<void>;
  showDefault?: boolean;
}

export interface WorkspaceView {
  render(): void;
  select(workspaceId: string): void;
  dispose(): void;
}

const availabilityPresentation: Record<
  Availability,
  { label: string; tone: StatusTone; icon: IconId }
> = {
  default: { label: '默认空间', tone: 'neutral', icon: 'icon-check' },
  unbound: { label: '未绑定目录', tone: 'warning', icon: 'icon-warning' },
  checking: { label: '正在检查', tone: 'running', icon: 'status-running' },
  available: { label: '目录可用', tone: 'success', icon: 'status-complete' },
  unavailable: { label: '目录不可用', tone: 'danger', icon: 'process-error' },
  unknown: { label: '未检查目录', tone: 'neutral', icon: 'process-clock' },
  error: { label: '目录状态未知', tone: 'warning', icon: 'icon-warning' },
};

const availabilityHelp: Partial<Record<Availability, string>> = {
  unbound: '关联一个本地目录后即可使用此工作空间。',
  unavailable: '保存的目录当前不存在或无法访问，请重新关联。',
  error: '应用暂时无法确认该目录是否可访问。可重新关联目录后重试。',
};

function workspacePath(workspace: Workspace): string {
  return workspace.root_path?.trim() ?? '';
}

function sessionCountLabel(count: number): string {
  return `${count} 个会话`;
}

function relinkLabel(state: Availability): string {
  return state === 'available' || state === 'checking' ? '更换目录' : '重新关联';
}

/**
 * Renders the shared Workspace navigator used by the management dialog and
 * Settings. Domain commands remain injected; this owner only projects state.
 */
export function createWorkspaceView(
  host: HTMLElement,
  options: WorkspaceViewOptions,
  initialWorkspaceId = 'default',
): WorkspaceView {
  const controller = new AbortController();
  const availability = new Map<string, AvailabilityRecord>();
  const controls = new Set<ButtonControl | FieldControl>();
  let selectedId = initialWorkspaceId;
  let query = '';
  let disposed = false;

  const root = document.createElement('section');
  const header = document.createElement('header');
  const heading = document.createElement('div');
  const title = document.createElement('h3');
  const summary = document.createElement('p');
  const create = createButton({
    label: '导入工作空间',
    icon: 'icon-folder',
    variant: 'primary',
    onPress: (event) => {
      void runAction(event.currentTarget as HTMLButtonElement, async () => {
        const createdId = await options.createWorkspace();
        if (typeof createdId === 'string') selectedId = createdId;
      });
    },
  });
  const searchWrap = document.createElement('div');
  const search = document.createElement('input');
  const clear = createIconButton({
    label: '清空工作空间搜索',
    icon: 'icon-close',
    size: 'small',
    onPress: () => {
      query = '';
      search.value = '';
      renderContent();
      search.focus();
    },
  });
  const content = document.createElement('div');
  const list = document.createElement('nav');
  const detail = document.createElement('div');
  const message = document.createElement('div');

  root.className = 'mw-workspace';
  root.dataset.workspaceView = '';
  root.setAttribute('aria-label', '工作空间');
  header.className = 'mw-workspace__header';
  heading.className = 'mw-workspace__heading';
  title.textContent = '工作空间';
  summary.textContent = '一个工作空间对应一个本地目录。';
  heading.append(title, summary);
  create.element.dataset.workspaceCreate = '';
  header.append(heading, create.element);
  searchWrap.className = 'mw-workspace__search';
  search.type = 'search';
  search.placeholder = '搜索工作空间';
  search.dataset.workspaceSearch = '';
  search.setAttribute('aria-label', '搜索工作空间');
  clear.element.hidden = true;
  searchWrap.append(createIcon('icon-search', { size: 16 }), search, clear.element);
  content.className = 'mw-workspace__content';
  list.className = 'mw-workspace__list';
  list.dataset.workspaceList = '';
  list.setAttribute('aria-label', '工作空间列表');
  detail.className = 'mw-workspace__detail';
  detail.setAttribute('aria-live', 'polite');
  message.className = 'mw-workspace__message';
  message.setAttribute('aria-live', 'polite');
  content.append(list, detail);
  root.append(header, searchWrap, message, content);
  host.replaceChildren(root);
  controls.add(create);
  controls.add(clear);

  const on = (
    target: EventTarget,
    type: string,
    listener: EventListenerOrEventListenerObject,
  ): void => target.addEventListener(type, listener, { signal: controller.signal });

  function allWorkspaces(): Workspace[] {
    const rows = [...options.getWorkspaces()];
    return options.showDefault === false
      ? rows.filter((workspace) => workspace.id !== 'default')
      : rows;
  }

  function stateFor(workspace: Workspace): Availability {
    if (workspace.id === 'default') return 'default';
    const path = workspacePath(workspace);
    if (!path) return 'unbound';
    const cached = availability.get(workspace.id);
    if (cached?.path === path) return cached.state;
    return options.directoryInfo ? 'checking' : 'unknown';
  }

  function setStatus(element: HTMLElement, state: Availability): void {
    const presentation = availabilityPresentation[state];
    element.className = 'mw-workspace__status';
    element.dataset.tone = presentation.tone;
    element.dataset.availability = state;
    element.replaceChildren(
      createIcon(presentation.icon, { size: 16 }),
      document.createTextNode(presentation.label),
    );
  }

  function setAvailabilityHelp(element: HTMLElement, state: Availability): void {
    const text = availabilityHelp[state] ?? '';
    element.textContent = text;
    element.hidden = !text;
  }

  function patchAvailability(workspaceId: string): void {
    const workspace = options.getWorkspaces().find((item) => item.id === workspaceId);
    if (!workspace) return;
    for (const element of root.querySelectorAll<HTMLElement>(
      `[data-workspace-availability="${CSS.escape(workspaceId)}"]`,
    )) {
      setStatus(element, stateFor(workspace));
    }
    for (const element of root.querySelectorAll<HTMLElement>(
      `[data-workspace-availability-help="${CSS.escape(workspaceId)}"]`,
    )) {
      setAvailabilityHelp(element, stateFor(workspace));
    }
    const relink = root.querySelector<HTMLButtonElement>(
      `[data-workspace-relink="${CSS.escape(workspaceId)}"]`,
    );
    const label = relink?.querySelector<HTMLElement>('.mw-button__label');
    if (label) label.textContent = relinkLabel(stateFor(workspace));
  }

  function checkAvailability(workspace: Workspace): void {
    const path = workspacePath(workspace);
    if (workspace.id === 'default' || !path || !options.directoryInfo) return;
    const cached = availability.get(workspace.id);
    if (cached?.path === path) return;
    availability.set(workspace.id, { path, state: 'checking' });
    patchAvailability(workspace.id);
    void options.directoryInfo(workspace.id).then(
      (info) => {
        if (disposed || workspacePath(
          options.getWorkspaces().find((item) => item.id === workspace.id) ?? workspace,
        ) !== path) return;
        availability.set(workspace.id, {
          path,
          state: info.exists ? 'available' : 'unavailable',
        });
        patchAvailability(workspace.id);
      },
      () => {
        if (disposed || workspacePath(
          options.getWorkspaces().find((item) => item.id === workspace.id) ?? workspace,
        ) !== path) return;
        availability.set(workspace.id, { path, state: 'error' });
        patchAvailability(workspace.id);
      },
    );
  }

  function clearDynamicControls(): void {
    for (const control of controls) {
      if (control === create || control === clear) continue;
      control.dispose();
      controls.delete(control);
    }
  }

  function renderLoadError(): void {
    const error = options.getLoadError();
    if (!error) return;
    message.dataset.tone = 'danger';
    message.dataset.workspaceError = '';
    message.setAttribute('role', 'status');
    const copy = document.createElement('span');
    copy.textContent = `工作空间加载失败：${error}`;
    message.append(copy);
    if (!options.reloadWorkspaces) return;
    const retry = createButton({
      label: '重新加载',
      variant: 'secondary',
      onPress: () => void runAction(retry.element, options.reloadWorkspaces!),
    });
    retry.element.dataset.workspaceReload = '';
    controls.add(retry);
    message.append(retry.element);
  }

  function actionButton(
    label: string,
    variant: 'primary' | 'secondary' | 'ghost' | 'danger',
    dataName: string,
    workspaceId: string,
    action: () => void | boolean | Promise<void | boolean>,
  ): HTMLButtonElement {
    const button = createButton({
      label,
      variant,
      onPress: () => void runAction(button.element, action),
    });
    button.element.dataset[dataName] = workspaceId;
    button.element.dataset.workspaceFocusKey = `${dataName}:${workspaceId}`;
    controls.add(button);
    return button.element;
  }

  async function runAction(
    button: HTMLButtonElement,
    action: () => void | boolean | Promise<void | boolean>,
  ): Promise<void> {
    if (disposed || button.disabled) return;
    message.replaceChildren();
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    try {
      const shouldRender = await action();
      if (disposed) return;
      if (shouldRender !== false) renderContent();
    } catch (error) {
      if (disposed) return;
      message.dataset.tone = 'danger';
      message.setAttribute('role', 'alert');
      message.textContent = error instanceof Error ? error.message : String(error);
    } finally {
      if (button.isConnected) {
        button.disabled = false;
        button.removeAttribute('aria-busy');
      }
    }
  }

  function createWorkspaceRow(workspace: Workspace): HTMLButtonElement {
    const button = document.createElement('button');
    const icon = document.createElement('span');
    const copy = document.createElement('span');
    const labelLine = document.createElement('span');
    const label = document.createElement('span');
    const metadata = document.createElement('span');
    const status = document.createElement('span');
    const count = document.createElement('span');

    button.type = 'button';
    button.className = 'mw-workspace__row';
    button.dataset.workspaceId = workspace.id;
    button.dataset.workspaceSelect = workspace.id;
    button.dataset.workspaceFocusKey = `workspace:${workspace.id}`;
    button.classList.toggle('is-selected', workspace.id === selectedId);
    if (workspace.id === selectedId) button.setAttribute('aria-current', 'true');
    icon.className = 'mw-workspace__row-icon';
    icon.append(createIcon(workspace.id === 'default' ? 'icon-task' : 'icon-folder', { size: 18 }));
    copy.className = 'mw-workspace__row-copy';
    labelLine.className = 'mw-workspace__row-label';
    label.textContent = workspace.name;
    labelLine.append(label);
    if (workspace.hidden) {
      const badge = document.createElement('span');
      badge.className = 'mw-workspace__badge';
      badge.textContent = '已隐藏';
      labelLine.append(badge);
    }
    metadata.className = 'mw-workspace__row-meta';
    status.dataset.workspaceAvailability = workspace.id;
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    setStatus(status, stateFor(workspace));
    count.textContent = sessionCountLabel(options.getSessionCount(workspace.id));
    count.dataset.workspaceSessionCount = workspace.id;
    metadata.append(status, count);
    copy.append(labelLine, metadata);
    button.append(icon, copy, createIcon('icon-chevron-down', { size: 16 }));
    return button;
  }

  function renderDetail(workspace: Workspace | undefined): void {
    detail.replaceChildren();
    if (!workspace) {
      const empty = document.createElement('div');
      empty.className = 'mw-workspace__empty';
      empty.textContent = '选择一个工作空间查看详情';
      detail.append(empty);
      return;
    }

    detail.dataset.workspaceDetail = workspace.id;
    const titleRow = document.createElement('div');
    const heading = document.createElement('div');
    const name = document.createElement('h3');
    const count = document.createElement('p');
    const rootBlock = document.createElement('div');
    const rootLabel = document.createElement('span');
    const rootValue = document.createElement('code');
    const status = document.createElement('span');
    const availabilityHint = document.createElement('p');
    const fields = document.createElement('div');
    const actions = document.createElement('div');
    const isDefault = workspace.id === 'default';

    titleRow.className = 'mw-workspace__detail-heading';
    name.textContent = workspace.name;
    count.textContent = sessionCountLabel(options.getSessionCount(workspace.id));
    count.dataset.workspaceSessionCount = workspace.id;
    heading.append(name, count);
    titleRow.append(heading);
    rootBlock.className = 'mw-workspace__root';
    rootLabel.textContent = '本地目录';
    rootValue.textContent = isDefault
      ? '默认工作空间不绑定本地目录'
      : workspacePath(workspace) || '尚未绑定本地目录';
    status.dataset.workspaceAvailability = workspace.id;
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    setStatus(status, stateFor(workspace));
    availabilityHint.className = 'mw-workspace__root-help';
    availabilityHint.dataset.workspaceAvailabilityHelp = workspace.id;
    setAvailabilityHelp(availabilityHint, stateFor(workspace));
    rootBlock.append(rootLabel, rootValue, status, availabilityHint);
    detail.append(titleRow, rootBlock);

    if (isDefault) return;

    const nameField = createField({
      kind: 'text',
      label: '名称',
      name: 'workspace-name',
      value: workspace.name,
      required: true,
    });
    const instructionsField = createField({
      kind: 'textarea',
      label: '项目提示词',
      name: 'workspace-instructions',
      value: workspace.instructions,
      helper: '在此工作空间的新会话中自动生效。',
      placeholder: '技术栈、代码规范、常用命令等',
    });
    controls.add(nameField);
    controls.add(instructionsField);
    nameField.control.dataset.workspaceFocusKey = `name:${workspace.id}`;
    instructionsField.control.dataset.workspaceFocusKey = `instructions:${workspace.id}`;
    fields.className = 'mw-workspace__fields';
    fields.append(nameField.element, instructionsField.element);
    actions.className = 'mw-workspace__detail-actions';

    actions.append(
      actionButton(
        relinkLabel(stateFor(workspace)),
        'secondary',
        'workspaceRelink',
        workspace.id,
        async () => {
          const nextId = await options.relinkWorkspace(workspace.id);
          if (typeof nextId === 'string') selectedId = nextId;
        },
      ),
      actionButton(
        workspace.hidden ? '取消隐藏' : '隐藏',
        'ghost',
        'workspaceHide',
        workspace.id,
        async () => options.setWorkspaceHidden(workspace.id, !workspace.hidden),
      ),
      actionButton(
        '删除',
        'danger',
        'workspaceDelete',
        workspace.id,
        async () => options.deleteWorkspace(workspace.id),
      ),
      actionButton(
        '保存',
        'primary',
        'workspaceSave',
        workspace.id,
        async () => {
          const nextName = nameField.control.value.trim();
          if (!nextName) {
            nameField.setError('请输入工作空间名称');
            nameField.control.focus();
            return false;
          }
          nameField.setError(null);
          await options.saveWorkspace(workspace.id, {
            name: nextName,
            instructions: instructionsField.control.value.trim(),
          });
          return true;
        },
      ),
    );
    detail.append(fields, actions);
  }

  function renderContent(): void {
    if (disposed) return;
    const active = document.activeElement as HTMLElement | null;
    const focusKey = active && root.contains(active) ? active.dataset.workspaceFocusKey : undefined;
    clear.element.hidden = !query;
    clearDynamicControls();
    message.replaceChildren();
    message.removeAttribute('role');
    message.removeAttribute('data-tone');
    message.removeAttribute('data-workspace-error');
    renderLoadError();

    const rows = allWorkspaces();
    if (!rows.some((workspace) => workspace.id === selectedId)) {
      selectedId = rows.find((workspace) => workspace.id === 'default')?.id ?? rows[0]?.id ?? '';
    }
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const filtered = normalizedQuery
      ? rows.filter((workspace) =>
        workspace.name.toLocaleLowerCase().includes(normalizedQuery)
        || workspacePath(workspace).toLocaleLowerCase().includes(normalizedQuery))
      : rows;
    const fragment = document.createDocumentFragment();
    if (filtered.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'mw-workspace__empty';
      if (normalizedQuery) {
        empty.dataset.workspaceFilteredEmpty = '';
        empty.textContent = '没有找到匹配的工作空间';
      } else if (options.getLoadError()) {
        empty.dataset.workspaceError = '';
        empty.setAttribute('role', 'status');
        empty.textContent = `工作空间加载失败：${options.getLoadError()}`;
      } else {
        empty.dataset.workspaceEmpty = '';
        empty.textContent = '尚无工作空间';
      }
      fragment.append(empty);
    } else {
      for (const workspace of filtered) fragment.append(createWorkspaceRow(workspace));
    }
    list.replaceChildren(fragment);
    renderDetail(rows.find((workspace) => workspace.id === selectedId));
    for (const workspace of rows) checkAvailability(workspace);
    if (focusKey) {
      const restored = root.querySelector<HTMLElement>(
        `[data-workspace-focus-key="${CSS.escape(focusKey)}"]`,
      );
      const fallback = root.querySelector<HTMLElement>(
        `[data-workspace-select="${CSS.escape(selectedId)}"]`,
      ) ?? search;
      (restored ?? fallback).focus();
    }
  }

  function select(workspaceId: string): void {
    if (!options.getWorkspaces().some((workspace) => workspace.id === workspaceId)) return;
    selectedId = workspaceId;
    renderContent();
  }

  function patchSessionCounts(): void {
    for (const element of root.querySelectorAll<HTMLElement>('[data-workspace-session-count]')) {
      const workspaceId = element.dataset.workspaceSessionCount;
      if (workspaceId) element.textContent = sessionCountLabel(options.getSessionCount(workspaceId));
    }
  }

  on(search, 'input', () => {
    query = search.value.trim();
    renderContent();
  });
  on(list, 'click', (event) => {
    const button = (event.target as Element).closest<HTMLButtonElement>('[data-workspace-select]');
    if (button?.dataset.workspaceSelect) select(button.dataset.workspaceSelect);
  });
  on(detail, 'keydown', (event) => {
    const keyboardEvent = event as KeyboardEvent;
    if (
      keyboardEvent.key !== 'Enter'
      || (!keyboardEvent.ctrlKey && !keyboardEvent.metaKey)
      || keyboardEvent.altKey
      || keyboardEvent.shiftKey
    ) return;
    const save = detail.querySelector<HTMLButtonElement>(
      `[data-workspace-save="${CSS.escape(selectedId)}"]`,
    );
    if (!save) return;
    keyboardEvent.preventDefault();
    save.click();
  });

  const unsubscribeWorkspace = workspaceStore.subscribe(() => renderContent());
  const unsubscribeSession = sessionStore.subscribe((next, previous) => {
    if (next.sessions !== previous.sessions) patchSessionCounts();
  });
  renderContent();

  return {
    render: renderContent,
    select,
    dispose() {
      if (disposed) return;
      disposed = true;
      controller.abort();
      unsubscribeWorkspace();
      unsubscribeSession();
      for (const control of controls) control.dispose();
      controls.clear();
      availability.clear();
      host.replaceChildren();
    },
  };
}
