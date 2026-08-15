import type { BrowserPageState } from '../backend-client';
import { createIcon, type IconId } from '../components/icon';

function isBlankUrl(value: string): boolean {
  const normalized = value.trim().toLowerCase();
  return normalized === '' || normalized === 'about:blank';
}

function tabTitle(tab: BrowserPageState['tabs'][number]): string {
  if (isBlankUrl(tab.url)) return '新标签页';
  return tab.title.trim() || tab.url.trim() || '新标签页';
}

function iconButton(
  label: string,
  icon: IconId,
  className: string,
): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.title = label;
  button.setAttribute('aria-label', label);
  button.appendChild(createIcon(icon, { size: 18 }));
  return button;
}

export function replaceBrowserTabs(
  strip: HTMLElement,
  value: BrowserPageState,
): void {
  strip.replaceChildren();
  if (value.tabs.length === 0) {
    const empty = document.createElement('span');
    empty.className = 'browser-tab browser-tab--empty';
    empty.textContent = '新标签页';
    strip.appendChild(empty);
    return;
  }
  for (const tab of value.tabs) {
    const item = document.createElement('div');
    item.className = `browser-tab${tab.id === value.tab_id ? ' is-active' : ''}`;
    item.setAttribute('role', 'presentation');
    const select = document.createElement('button');
    select.type = 'button';
    select.className = 'browser-tab__select';
    select.dataset.browserTab = tab.id;
    select.title = tabTitle(tab);
    select.setAttribute('role', 'tab');
    select.setAttribute('aria-selected', String(tab.id === value.tab_id));
    select.appendChild(createIcon('process-web', { size: 16 }));
    const label = document.createElement('span');
    label.textContent = tabTitle(tab);
    select.appendChild(label);
    const close = iconButton('关闭标签页', 'icon-close', 'browser-tab__close');
    close.dataset.browserCloseTab = tab.id;
    item.append(select, close);
    strip.appendChild(item);
  }
}

export function createBrowserInspector(
  value: BrowserPageState,
  options: { maximized: boolean; nativeViewMounted: boolean },
): HTMLElement {
  const hasPage = Boolean(value.tab_id);
  const blankPage = !hasPage || isBlankUrl(value.url);

  const root = document.createElement('section');
  root.className = 'browser-panel mw-browser-inspector';
  root.dataset.browserPanel = '';
  root.setAttribute('aria-label', 'Crew 应用内浏览器');

  const tabBar = document.createElement('div');
  tabBar.className = 'browser-tabbar';
  const strip = document.createElement('div');
  strip.className = 'browser-tab-strip';
  strip.dataset.browserTabStrip = '';
  strip.setAttribute('role', 'tablist');
  strip.setAttribute('aria-label', '当前会话标签页');
  replaceBrowserTabs(strip, value);
  const add = iconButton('新建标签页', 'icon-plus', 'browser-chrome-btn');
  add.dataset.browserNewTab = '';
  const maximize = iconButton(
    options.maximized ? '还原浏览器' : '展开浏览器',
    'icon-expand',
    'browser-chrome-btn',
  );
  maximize.dataset.browserShell = 'maximize';
  maximize.setAttribute('aria-pressed', String(options.maximized));
  const close = iconButton('关闭浏览器面板', 'icon-close', 'browser-chrome-btn');
  close.dataset.browserShell = 'close';
  tabBar.append(strip, add, maximize, close);

  const toolbar = document.createElement('div');
  toolbar.className = 'browser-toolbar';
  const navigation = document.createElement('div');
  navigation.className = 'browser-nav';
  navigation.setAttribute('role', 'toolbar');
  navigation.setAttribute('aria-label', '浏览器导航');
  const back = iconButton('后退', 'icon-back', 'browser-icon-btn');
  back.dataset.browserAction = 'back';
  back.disabled = !hasPage || !value.can_go_back;
  const forward = iconButton('前进', 'icon-back', 'browser-icon-btn browser-icon-btn--forward');
  forward.dataset.browserAction = 'forward';
  forward.disabled = !hasPage || !value.can_go_forward;
  const reload = iconButton('刷新', 'icon-refresh', 'browser-icon-btn');
  reload.dataset.browserAction = 'reload';
  reload.disabled = !hasPage || blankPage;
  navigation.append(back, forward, reload);
  const address = document.createElement('input');
  address.className = 'browser-url';
  address.dataset.browserUrl = '';
  address.type = 'text';
  address.value = blankPage ? '' : value.url;
  address.setAttribute('aria-label', '网页地址');
  address.placeholder = '输入网址或搜索内容';
  address.autocomplete = 'off';
  address.spellcheck = false;
  address.inputMode = 'url';
  address.readOnly = value.mode !== 'human' && hasPage;

  const note = document.createElement('form');
  note.className = 'browser-note';
  note.dataset.browserNote = '';
  note.hidden = true;
  const noteInput = document.createElement('input');
  noteInput.className = 'browser-note__input';
  noteInput.dataset.browserNoteInput = '';
  noteInput.type = 'text';
  noteInput.autocomplete = 'off';
  noteInput.setAttribute('aria-label', '给这一步加说明');
  noteInput.placeholder = '这一步要说明什么？例如：这个工单号每次都不同';
  const saveNote = document.createElement('button');
  saveNote.type = 'submit';
  saveNote.className = 'browser-note__btn browser-note__btn--primary';
  saveNote.textContent = '保存';
  const cancelNote = document.createElement('button');
  cancelNote.type = 'button';
  cancelNote.className = 'browser-note__btn';
  cancelNote.dataset.browserNoteCancel = '';
  cancelNote.textContent = '取消';
  note.append(noteInput, saveNote, cancelNote);

  const recordingStatus = document.createElement('div');
  recordingStatus.className = 'browser-recording';
  recordingStatus.dataset.browserRecording = '';
  recordingStatus.setAttribute('role', 'status');
  recordingStatus.setAttribute('aria-live', 'polite');
  recordingStatus.hidden = true;

  const recordingControls = document.createElement('span');
  recordingControls.className = 'browser-rec-slot';
  recordingControls.dataset.browserRecControls = '';
  const recordStart = document.createElement('button');
  recordStart.type = 'button';
  recordStart.className = 'browser-icon-btn browser-icon-btn--record';
  recordStart.dataset.browserRecord = 'start';
  recordStart.setAttribute('aria-label', '开始录制技能');
  recordStart.title = '开始录制技能 · 把这段操作录成可重放的技能';
  recordStart.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="6" fill="currentColor" stroke="none"/></svg>';
  recordingControls.append(recordStart);
  toolbar.append(navigation, address, note, recordingStatus, recordingControls);

  const takeover = document.createElement('div');
  takeover.className = 'browser-takeover';
  takeover.dataset.browserTakeover = '';
  takeover.setAttribute('role', 'alert');
  takeover.hidden = true;
  const takeoverText = document.createElement('span');
  takeoverText.className = 'browser-takeover__text';
  takeoverText.textContent = '检测到页面操作，是否接管浏览器？';
  const takeoverConfirm = document.createElement('button');
  takeoverConfirm.type = 'button';
  takeoverConfirm.className = 'browser-takeover__btn browser-takeover__btn--primary';
  takeoverConfirm.dataset.browserTakeoverAction = 'confirm';
  takeoverConfirm.textContent = '接管';
  const takeoverDismiss = document.createElement('button');
  takeoverDismiss.type = 'button';
  takeoverDismiss.className = 'browser-takeover__btn';
  takeoverDismiss.dataset.browserTakeoverAction = 'dismiss';
  takeoverDismiss.textContent = '忽略';
  takeover.append(takeoverText, takeoverConfirm, takeoverDismiss);

  const stage = document.createElement('div');
  stage.className = `browser-stage${value.mode === 'human' ? ' is-interactive' : ''}`;
  stage.dataset.browserStage = '';
  stage.setAttribute('aria-label', '沙箱浏览器视图');
  const empty = document.createElement('div');
  empty.className = 'browser-empty';
  empty.dataset.browserEmpty = '';
  empty.hidden = hasPage && !blankPage && options.nativeViewMounted;
  const emptyIcon = document.createElement('span');
  emptyIcon.className = 'browser-empty__icon';
  emptyIcon.appendChild(createIcon('process-web', { size: 40 }));
  const emptyTitle = document.createElement('strong');
  emptyTitle.dataset.browserEmptyTitle = '';
  emptyTitle.textContent = blankPage ? '开始浏览' : '正在打开页面…';
  const emptyDescription = document.createElement('span');
  emptyDescription.dataset.browserEmptyDescription = '';
  emptyDescription.textContent = blankPage ? '输入 URL 以打开页面' : '页面加载后将在此显示';
  empty.append(emptyIcon, emptyTitle, emptyDescription);
  const status = document.createElement('div');
  status.className = 'browser-status';
  status.dataset.browserStatus = '';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  const loadError = document.createElement('div');
  loadError.className = 'browser-load-error';
  loadError.dataset.browserLoadError = '';
  loadError.hidden = true;
  const loadErrorTitle = document.createElement('strong');
  loadErrorTitle.textContent = '页面加载失败';
  const loadErrorUrl = document.createElement('span');
  loadErrorUrl.dataset.browserLoadErrorUrl = '';
  const loadErrorDesc = document.createElement('span');
  loadErrorDesc.dataset.browserLoadErrorDescription = '';
  const loadErrorRetry = document.createElement('button');
  loadErrorRetry.type = 'button';
  loadErrorRetry.className = 'browser-load-error__retry';
  loadErrorRetry.dataset.browserLoadRetry = '';
  loadErrorRetry.textContent = '重试';
  loadError.append(loadErrorTitle, loadErrorUrl, loadErrorDesc, loadErrorRetry);
  stage.append(empty, loadError, status);
  root.append(tabBar, toolbar, takeover, stage);
  return root;
}
