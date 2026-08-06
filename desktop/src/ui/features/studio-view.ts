import { createIcon, type IconId } from '../components/icon';

export type StudioAvailability = 'loading' | 'ready' | 'unavailable';

function studioButton(panel: 'chat' | 'history', label: string, icon: IconId): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'studio-toolbar-btn';
  button.dataset.studioPanel = panel;
  button.title = label;
  button.setAttribute('aria-label', label);
  button.setAttribute('aria-pressed', 'false');
  button.append(createIcon(icon, { size: 18 }));
  return button;
}

function studioPane(id: string): HTMLDivElement {
  const pane = document.createElement('div');
  pane.id = id;
  pane.className = 'studio-panel-pane';
  pane.hidden = true;
  return pane;
}

function createStudioView(): HTMLDivElement {
  const stage = document.createElement('div');
  stage.id = 'studio-stage';
  stage.className = 'studio-stage mw-studio';
  stage.dataset.availability = 'loading';
  stage.setAttribute('aria-hidden', 'true');

  const frame = document.createElement('iframe');
  frame.id = 'studio-webview';
  frame.className = 'studio-stage-webview';
  frame.src = 'about:blank';
  frame.title = '工作室';

  const status = document.createElement('div');
  status.id = 'studio-stage-status';
  status.className = 'studio-stage-status';
  status.setAttribute('role', 'status');

  const statusMessage = document.createElement('span');
  statusMessage.id = 'studio-stage-status-message';
  statusMessage.textContent = '正在加载工作室...';

  const returnButton = document.createElement('button');
  returnButton.id = 'studio-return-chat';
  returnButton.className = 'studio-return-chat';
  returnButton.type = 'button';
  returnButton.hidden = true;
  returnButton.append(createIcon('icon-back', { size: 16 }), document.createTextNode('返回对话'));
  status.append(statusMessage, returnButton);

  const chrome = document.createElement('div');
  chrome.id = 'studio-chrome';
  chrome.className = 'studio-chrome';
  chrome.hidden = true;

  const stack = document.createElement('div');
  stack.className = 'studio-panel-stack';

  const toolbar = document.createElement('div');
  toolbar.className = 'studio-toolbar';
  toolbar.setAttribute('role', 'toolbar');
  toolbar.setAttribute('aria-label', '工作室工具');
  toolbar.append(
    studioButton('chat', '对话', 'icon-task'),
    studioButton('history', '历史记录', 'process-clock'),
  );

  const sidePanel = document.createElement('aside');
  sidePanel.id = 'studio-side-panel';
  sidePanel.className = 'studio-side-panel';
  sidePanel.setAttribute('aria-label', '工作室侧栏');
  sidePanel.hidden = true;

  const collapse = document.createElement('button');
  collapse.id = 'studio-panel-collapse';
  collapse.className = 'studio-panel-collapse';
  collapse.type = 'button';
  collapse.title = '收起面板';
  collapse.setAttribute('aria-label', '收起面板');
  collapse.append(createIcon('icon-chevron-down', { size: 16 }));

  const panelBody = document.createElement('div');
  panelBody.className = 'studio-panel-body';

  const chatPane = studioPane('studio-panel-chat');
  const messages = document.createElement('div');
  messages.id = 'studio-chat-messages';
  messages.className = 'studio-chat-messages web-flow';
  chatPane.append(messages);

  const historyPane = studioPane('studio-panel-history');
  const searchWrap = document.createElement('label');
  searchWrap.id = 'studio-history-search-wrap';
  searchWrap.className = 'studio-history-search-wrap';
  searchWrap.append(createIcon('icon-search', { size: 16 }));

  const search = document.createElement('input');
  search.id = 'studio-history-search';
  search.className = 'studio-history-search';
  search.type = 'search';
  search.placeholder = '搜索';
  search.autocomplete = 'off';
  search.setAttribute('aria-label', '搜索历史记录');
  searchWrap.append(search);

  const historyLabel = document.createElement('div');
  historyLabel.className = 'studio-history-section-label';
  historyLabel.textContent = '最近一个月';

  const historyList = document.createElement('div');
  historyList.id = 'studio-history-list';
  historyList.className = 'studio-history-list';
  historyPane.append(searchWrap, historyLabel, historyList);

  panelBody.append(chatPane, historyPane);
  sidePanel.append(collapse, panelBody);
  stack.append(toolbar, sidePanel);
  chrome.append(stack);
  stage.append(frame, status, chrome);
  return stage;
}

/** Ensures that the chat workspace has one runtime-owned Studio surface. */
export function ensureStudioView(): HTMLDivElement {
  const existing = document.getElementById('studio-stage') as HTMLDivElement | null;
  if (existing) return existing;
  const chatTab = document.getElementById('chat-tab');
  if (!chatTab) throw new Error('Studio requires #chat-tab');
  const stage = createStudioView();
  chatTab.prepend(stage);
  return stage;
}

/** Updates the visible resource state without changing the document theme. */
export function setStudioAvailability(
  availability: StudioAvailability,
  message?: string,
): void {
  const stage = ensureStudioView();
  const status = stage.querySelector<HTMLElement>('#studio-stage-status');
  const statusMessage = stage.querySelector<HTMLElement>('#studio-stage-status-message');
  const returnButton = stage.querySelector<HTMLButtonElement>('#studio-return-chat');
  const fallback = availability === 'loading'
    ? '正在加载工作室...'
    : '工作室当前不可用';

  stage.dataset.availability = availability;
  if (status) status.hidden = availability === 'ready';
  if (statusMessage) statusMessage.textContent = message ?? fallback;
  if (returnButton) returnButton.hidden = availability !== 'unavailable';
}
