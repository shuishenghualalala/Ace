import { createIcon } from '../components/icon';

export type InspectorTabKey =
  | 'context'
  | 'files'
  | 'plan'
  | 'kanban'
  | 'collaboration'
  | 'browser';

/** Builds the Inspector chrome; the feature owns the dynamic workspace tabs. */
export function mountInspectorShell(root: HTMLElement): void {
  root.replaceChildren();
  root.classList.add('mw-inspector');
  root.setAttribute('aria-label', '会话检查器');

  const resize = document.createElement('div');
  resize.id = 'chat-inspector-resize-handle';
  resize.className = 'chat-inspector__resize-handle';
  resize.title = '拖拽调整检查器宽度';
  resize.setAttribute('aria-hidden', 'true');

  const header = document.createElement('header');
  header.className = 'chat-inspector__head mw-inspector__header';
  const tabs = document.createElement('div');
  tabs.className = 'chat-inspector__tabs mw-inspector__tabs';
  tabs.id = 'chat-inspector-tabs';
  tabs.setAttribute('role', 'tablist');
  tabs.setAttribute('aria-label', '检查器视图');

  const picker = document.createElement('div');
  picker.className = 'chat-inspector__tab-picker mw-inspector__tab-picker';
  const add = document.createElement('button');
  add.id = 'inspector-new-browser-tab';
  add.type = 'button';
  add.className = 'chat-inspector__chrome-btn mw-inspector__chrome-btn';
  add.setAttribute('aria-label', '新增页面');
  add.setAttribute('aria-expanded', 'false');
  add.title = '新增页面';
  add.appendChild(createIcon('icon-plus', { size: 18 }));
  const pickerToggle = document.createElement('button');
  pickerToggle.id = 'inspector-tab-picker-toggle';
  pickerToggle.type = 'button';
  pickerToggle.className = 'chat-inspector__chrome-btn mw-inspector__chrome-btn';
  pickerToggle.hidden = true;
  pickerToggle.setAttribute('aria-label', '已打开页面');
  pickerToggle.setAttribute('aria-expanded', 'false');
  pickerToggle.appendChild(createIcon('icon-chevron-down', { size: 16 }));
  const menu = document.createElement('div');
  menu.id = 'inspector-tab-menu';
  menu.className = 'chat-inspector__tab-menu mw-inspector__tab-menu';
  menu.hidden = true;
  menu.setAttribute('role', 'menu');
  picker.append(add, pickerToggle, menu);

  const maximize = document.createElement('button');
  maximize.id = 'inspector-maximize';
  maximize.type = 'button';
  maximize.className = 'chat-inspector__chrome-btn mw-inspector__chrome-btn';
  maximize.setAttribute('aria-label', '放大看板');
  maximize.setAttribute('aria-pressed', 'false');
  maximize.title = '放大看板';
  maximize.appendChild(createIcon('icon-expand', { size: 16 }));
  const close = document.createElement('button');
  close.id = 'inspector-close';
  close.type = 'button';
  close.className = 'chat-inspector__chrome-btn mw-inspector__chrome-btn mw-inspector__close';
  close.setAttribute('aria-label', '关闭看板');
  close.title = '关闭看板';
  close.appendChild(createIcon('icon-close', { size: 18 }));
  header.append(tabs, picker, maximize, close);

  const body = document.createElement('div');
  body.id = 'chat-inspector-body';
  body.className = 'chat-inspector__body mw-inspector__body';
  body.setAttribute('role', 'tabpanel');
  body.setAttribute('aria-live', 'polite');
  const workspace = document.createElement('div');
  workspace.className = 'chat-inspector__workspace mw-inspector__workspace';
  workspace.appendChild(body);
  root.append(resize, header, workspace);
}
