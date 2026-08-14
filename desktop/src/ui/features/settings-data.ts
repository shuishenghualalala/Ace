import { createIcon, type IconId } from '../components/icon';

function createPane(id: string, titleText: string, descriptionText: string): {
  pane: HTMLElement;
  body: HTMLElement;
} | null {
  const pane = document.getElementById(id);
  if (!pane) return null;
  const existing = pane.querySelector<HTMLElement>('[data-settings-data-pane]');
  if (existing) {
    return { pane, body: existing.querySelector<HTMLElement>('[data-settings-data-body]')! };
  }
  const shell = document.createElement('section');
  const header = document.createElement('header');
  const title = document.createElement('h1');
  const description = document.createElement('p');
  const body = document.createElement('div');
  shell.className = 'settings-data';
  shell.dataset.settingsDataPane = '';
  header.className = 'settings-data__header';
  title.className = 'settings-data__title';
  title.textContent = titleText;
  description.className = 'settings-data__description';
  description.textContent = descriptionText;
  body.className = 'settings-data__body';
  body.dataset.settingsDataBody = '';
  header.append(title, description);
  shell.append(header, body);
  pane.replaceChildren(shell);
  return { pane, body };
}

function createLibrarySection(options: {
  id: 'projects' | 'sessions';
  title: string;
  description: string;
  icon: IconId;
}): HTMLElement {
  const section = document.createElement('section');
  const toggle = document.createElement('button');
  const symbol = document.createElement('span');
  const copy = document.createElement('span');
  const title = document.createElement('strong');
  const description = document.createElement('span');
  const count = document.createElement('span');
  const panel = document.createElement('div');
  const root = document.createElement('div');
  section.className = 'library-accordion-item is-open';
  section.dataset.librarySection = options.id;
  toggle.type = 'button';
  toggle.className = 'library-accordion-header';
  toggle.dataset.libraryToggle = options.id;
  toggle.setAttribute('aria-expanded', 'true');
  toggle.setAttribute('aria-controls', `settings-library-${options.id}-panel`);
  symbol.className = 'library-accordion-header__icon';
  symbol.append(createIcon(options.icon, { size: 18 }));
  copy.className = 'library-accordion-header__copy';
  title.className = 'library-accordion-header__title';
  title.textContent = options.title;
  description.className = 'library-accordion-header__desc';
  description.textContent = options.description;
  copy.append(title, description);
  count.id = `settings-library-${options.id}-count`;
  count.className = 'library-accordion-header__count';
  count.textContent = '0';
  toggle.append(symbol, copy, count, createIcon('icon-chevron-down', { size: 16 }));
  panel.id = `settings-library-${options.id}-panel`;
  panel.className = 'library-accordion-panel';
  panel.dataset.libraryPanel = options.id;
  root.id = `settings-library-${options.id}`;
  root.className = 'library-accordion-panel__inner';
  panel.append(root);
  section.append(toggle, panel);
  return section;
}

function createDataAction(options: {
  title: string;
  description: string;
  buttonId: string;
  buttonLabel: string;
  danger?: boolean;
}): HTMLElement {
  const row = document.createElement('article');
  const copy = document.createElement('div');
  const title = document.createElement('strong');
  const description = document.createElement('p');
  const button = document.createElement('button');
  row.className = 'settings-data__action';
  copy.className = 'settings-data__action-copy';
  title.textContent = options.title;
  description.textContent = options.description;
  button.id = options.buttonId;
  button.type = 'button';
  button.className = options.danger
    ? 'mw-button mw-button--danger mw-button--default'
    : 'mw-button mw-button--secondary mw-button--default';
  button.textContent = options.buttonLabel;
  copy.append(title, description);
  row.append(copy, button);
  return row;
}

export function mountSettingsDataPanes(): void {
  const logs = createPane(
    'settings-pane-sys-logs',
    '系统日志',
    '查看本地服务日志，按级别和关键词筛选。',
  );
  if (logs && !logs.body.querySelector('#sys-logs-list')) {
    const toolbar = document.createElement('div');
    const level = document.createElement('select');
    const searchWrap = document.createElement('label');
    const search = document.createElement('input');
    const auto = document.createElement('label');
    const autoInput = document.createElement('input');
    const autoTrack = document.createElement('span');
    const autoThumb = document.createElement('span');
    const meta = document.createElement('div');
    const count = document.createElement('span');
    const countValue = document.createElement('strong');
    const refresh = document.createElement('button');
    const feed = document.createElement('div');
    const pager = document.createElement('div');
    toolbar.className = 'settings-data__toolbar';
    level.id = 'sys-logs-level';
    level.className = 'mw-field__control settings-data__level';
    level.setAttribute('aria-label', '日志级别');
    for (const value of ['', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = value || '全部级别';
      level.append(option);
    }
    searchWrap.className = 'settings-data__search';
    searchWrap.setAttribute('aria-label', '搜索日志');
    search.id = 'sys-logs-keyword';
    search.className = 'mw-field__control settings-data__search-input';
    search.type = 'search';
    search.placeholder = '搜索日志关键词';
    auto.className = 'settings-data__auto';
    autoInput.id = 'sys-logs-auto-refresh';
    autoInput.type = 'checkbox';
    autoInput.setAttribute('role', 'switch');
    autoTrack.className = 'settings-data__switch-track';
    autoThumb.className = 'settings-data__switch-thumb';
    autoTrack.append(autoThumb);
    auto.append(autoInput, autoTrack, document.createTextNode('自动刷新'));
    searchWrap.append(createIcon('icon-search', { size: 16 }), search);
    meta.className = 'settings-data__toolbar-meta';
    count.className = 'settings-data__count';
    countValue.id = 'sys-logs-count';
    countValue.textContent = '0';
    count.append('共 ', countValue, ' 条');
    refresh.id = 'sys-logs-refresh';
    refresh.type = 'button';
    refresh.className = 'mw-button mw-button--secondary mw-button--small mw-button--icon';
    refresh.setAttribute('aria-label', '刷新日志');
    refresh.title = '刷新日志';
    refresh.append(createIcon('icon-refresh', { size: 16 }));
    meta.append(auto, count, refresh);
    toolbar.append(level, searchWrap, meta);
    feed.id = 'sys-logs-list';
    feed.className = 'settings-data__logs';
    feed.setAttribute('aria-live', 'polite');
    pager.id = 'sys-logs-pager';
    pager.className = 'settings-data__pager';
    logs.body.append(toolbar, feed, pager);
  }

  const usage = createPane(
    'settings-pane-sys-usage',
    '使用统计',
    '查看 Token 用量、会话与模型消耗明细。',
  );
  if (usage && !usage.body.querySelector('#usage-page-root')) {
    const root = document.createElement('div');
    root.id = 'usage-page-root';
    root.className = 'usage-page-root';
    usage.body.append(root);
  }

  const library = createPane(
    'settings-pane-library',
    '资源库',
    '管理本地工作空间与归档会话。',
  );
  if (library && !library.body.querySelector('#settings-library-accordion')) {
    const accordion = document.createElement('div');
    accordion.id = 'settings-library-accordion';
    accordion.className = 'settings-library-accordion';
    accordion.append(
      createLibrarySection({
        id: 'projects',
        title: '工作空间',
        description: '已注册的本地工作目录',
        icon: 'icon-folder',
      }),
      createLibrarySection({
        id: 'sessions',
        title: '归档会话',
        description: '已从主列表隐藏的历史对话',
        icon: 'icon-file',
      }),
    );
    library.body.append(accordion);
  }

  const data = createPane(
    'settings-pane-data',
    '数据管理',
    '管理本地缓存、会话导出和应用设置。',
  );
  if (data && !data.body.querySelector('#set-clear-cache')) {
    const actions = document.createElement('div');
    actions.className = 'settings-data__actions-list';
    actions.append(
      createDataAction({
        title: '清除缓存',
        description: '清空本地草稿与未发送的附件。',
        buttonId: 'set-clear-cache',
        buttonLabel: '清除',
      }),
      createDataAction({
        title: '导出全部会话',
        description: '导出所有会话元数据与完整消息正文。',
        buttonId: 'set-export-sessions',
        buttonLabel: '导出',
      }),
      createDataAction({
        title: '重置全部设置',
        description: '恢复默认设置，会话与登录状态保持不变。',
        buttonId: 'set-reset-all',
        buttonLabel: '重置',
        danger: true,
      }),
    );
    data.body.append(actions);
  }
}
