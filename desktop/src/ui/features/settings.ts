/**
 * 设置弹窗：侧栏齿轮按钮触发；内部"左 tab + 右表单"。
 *
 * 行为持久化：所有控件写入 localStorage `crew.settings.*`，实时应用主题/字体大小。
 * 真实行为：
 *   - 主题模式：写入 root/body theme 类与 data 属性，system 跟随 OS 深浅色变化
 *   - 字体大小：三项用户设置覆盖 UI / 正文 / 代码；编辑输入复用正文字号
 *   - 关闭行为：仅记录偏好（实际生效需 Electron 主进程配合）
 *   - 清除缓存：扫描 localStorage 中 crew.* 命名的 key
 *   - 导出会话：读取后端 /api/sessions 并下载为 JSON
 */

import { backendApi } from '../backend-client';
import { openDialog, type OverlayHandle } from '../components/overlays';
import { clearRuntimeToken, setRuntimeToken } from '../components/runtime-style';
import { renderMarkdownHtml } from '../markdown';
import helpDocMarkdown from '../../../assets/help-docs/crew-user-guide.md';
import {
  bindSettingsLibraryUi,
  disposeSettingsLibraryPane,
} from './settings-library';
import { bindSessionPreviewModal } from './session-preview-modal';
import { bindContactModal, closeContactModal, openContactModal } from './settings-contact';
import { $, notify } from '../state';
import { requireRendererLogin } from './auth-gate';
import { renderAuthAccount } from './login';
import {
  DEFAULT_SETTINGS,
  clampNumber,
  hydrateSettings,
  resolveFontFamilyOverride,
  type Settings,
} from './settings-preferences';
import { createSettingsShell, type SettingsShell } from './settings-shell';
import { mountSettingsDataPanes } from './settings-data';
import { disposeMcpPane } from './settings-mcp';
import { productModeStore } from '../stores/product-mode-store';

const STORAGE_KEY = 'crew.settings';
const ROOT = document.documentElement;
const HELP_DOC_ASSET_BASE = './help-docs/';
const HELP_DOC_VERSION_LABEL = `文档版本 ${__HELP_DOC_VERSION__}`;

const SYSTEM_THEME_QUERY = '(prefers-color-scheme: dark)';
const systemThemeMedia = window.matchMedia?.(SYSTEM_THEME_QUERY) ?? null;

function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return hydrateSettings(JSON.parse(raw) as Partial<Settings> & { fontSize?: number });
  } catch {
    /* ignore */
  }
  return { ...DEFAULT_SETTINGS };
}

function saveSettings(s: Settings): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
  } catch {
    /* quota or disabled */
  }
}

let current: Settings = loadSettings();
let settingsShell: SettingsShell | null = null;
let helpDocOverlay: OverlayHandle<HTMLDivElement> | null = null;

interface HelpDocHeading {
  id: string;
  level: number;
  title: string;
  searchText: string;
}

function helpDocHeadings(source: string): HelpDocHeading[] {
  const headings: HelpDocHeading[] = [];
  for (const line of source.split(/\r?\n/)) {
    const match = /^(#{1,3})\s+(.+)$/.exec(line);
    if (!match) continue;
    const title = match[2]
      .replace(/\\([+_.()])/g, '$1')
      .replace(/\*\*(.*?)\*\*/g, '$1')
      .trim();
    headings.push({
      id: `help-doc-heading-${headings.length}`,
      level: match[1].length,
      title,
      searchText: title.toLowerCase(),
    });
  }
  return headings;
}

function openHelpDoc(trigger: HTMLElement): void {
  helpDocOverlay?.close();
  const headings = helpDocHeadings(helpDocMarkdown);
  const layout = document.createElement('div');
  layout.className = 'help-doc-modal__body';
  const sidebar = document.createElement('aside');
  sidebar.className = 'help-doc-sidebar';
  sidebar.setAttribute('aria-label', '帮助文档目录');
  const searchBox = document.createElement('label');
  searchBox.className = 'help-doc-search';
  searchBox.innerHTML = `
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="m21 21-4.35-4.35"></path>
      <circle cx="11" cy="11" r="7"></circle>
    </svg>
  `;
  const search = document.createElement('input');
  search.type = 'search';
  search.placeholder = '搜索文档';
  search.autocomplete = 'off';
  search.setAttribute('aria-label', '搜索帮助文档');
  searchBox.append(search);
  const treeHeading = document.createElement('div');
  treeHeading.className = 'help-doc-sidebar__head';
  const treeTitle = document.createElement('span');
  treeTitle.textContent = '目录';
  const chapterCount = document.createElement('span');
  chapterCount.textContent = `${headings.filter((item) => item.level === 1).length}章`;
  treeHeading.append(treeTitle, chapterCount);
  const tree = document.createElement('nav');
  tree.className = 'help-doc-tree';
  tree.setAttribute('aria-label', '文档章节');
  const empty = document.createElement('div');
  empty.className = 'help-doc-empty';
  empty.textContent = '没有匹配的目录';
  empty.hidden = true;
  const version = document.createElement('div');
  version.className = 'help-doc-sidebar__version';
  version.textContent = HELP_DOC_VERSION_LABEL;
  const content = document.createElement('article');
  content.className = 'help-doc-content md-body chat-markdown';
  content.setAttribute('aria-label', '帮助文档内容');
  const markdown = helpDocMarkdown.replace(
    /!\[([^\]]*)\]\((image(?:-\d+)?\.png)\)/g,
    (_all: string, alt: string, filename: string) =>
      `![${alt || 'Crew 功能截图'}](${HELP_DOC_ASSET_BASE}${filename})`,
  );
  content.innerHTML = renderMarkdownHtml(markdown, { allowImages: true });
  content.querySelectorAll<HTMLElement>('h1, h2, h3').forEach((heading, index) => {
    if (headings[index]) heading.id = headings[index].id;
  });
  const renderTree = (): void => {
    const query = search.value.trim().toLowerCase();
    tree.replaceChildren();
    const matches = headings.filter((item) => !query || item.searchText.includes(query));
    empty.hidden = matches.length > 0;
    for (const heading of matches) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `help-doc-tree__item help-doc-tree__item--level-${heading.level}`;
      button.textContent = heading.title;
      button.addEventListener('click', () => {
        content.querySelector<HTMLElement>(`#${heading.id}`)?.scrollIntoView({
          block: 'start',
          behavior: 'smooth',
        });
      });
      tree.append(button);
    }
  };
  search.addEventListener('input', renderTree);
  renderTree();
  sidebar.append(searchBox, treeHeading, tree, empty, version);
  layout.append(sidebar, content);
  helpDocOverlay = openDialog({
    trigger,
    title: '帮助文档',
    content: layout,
    onClose: () => {
      helpDocOverlay = null;
    },
  });
  helpDocOverlay.element.querySelector('.mw-dialog')?.classList.add('help-doc-modal-content');
}

async function syncAutoStartFromSystem(): Promise<void> {
  try {
    const result = await window.Crew?.getAutoLaunchEnabled?.();
    if (!result || typeof result.enabled !== 'boolean') return;
    if (current.autoStart === result.enabled) return;
    current.autoStart = result.enabled;
    saveSettings(current);
    const autoStart = document.querySelector<HTMLInputElement>('#set-auto-start');
    if (autoStart) autoStart.checked = result.enabled;
  } catch {
    // 主进程不可用时保留本地偏好，避免设置面板无法打开。
  }
}

async function syncCloseBehaviorFromMain(): Promise<void> {
  try {
    const result = await window.Crew?.getCloseBehavior?.();
    const behavior = result?.closeBehavior;
    if (behavior !== 'tray' && behavior !== 'quit' && behavior !== 'ask') return;
    if (current.closeBehavior === behavior) return;
    current.closeBehavior = behavior;
    saveSettings(current);
    const closeSel = document.querySelector<HTMLSelectElement>('#set-close-behavior');
    if (closeSel) closeSel.value = behavior;
  } catch {
    // 主进程不可用时保留本地偏好。
  }
}

async function syncStrictSecurityFromMain(): Promise<void> {
  try {
    const result = await window.Crew?.getStrictSecurityEnabled?.();
    if (!result || typeof result.strictSecurityEnabled !== 'boolean') return;
    const toggle = document.querySelector<HTMLInputElement>('#set-strict-security');
    if (toggle) toggle.checked = result.strictSecurityEnabled;
  } catch {
    // 主进程不可用时保留 fail-closed 的 checked 默认值。
  }
}

function updateRangeValue(id: string, px: number): void {
  const el = document.getElementById(id);
  if (el) el.textContent = `${px}px`;
}

function applyFontSizes(s: Settings): void {
  const ui = clampNumber(s.uiFontSize, 12, 18, DEFAULT_SETTINGS.uiFontSize);
  const content = clampNumber(s.contentFontSize, 12, 20, DEFAULT_SETTINGS.contentFontSize);
  const terminal = clampNumber(s.terminalFontSize, 11, 18, DEFAULT_SETTINGS.terminalFontSize);

  current.uiFontSize = ui;
  current.contentFontSize = content;
  current.terminalFontSize = terminal;

  setRuntimeToken(ROOT, '--mw-font-ui-size', `${ui}px`);
  setRuntimeToken(ROOT, '--mw-font-content-size', `${content}px`);
  setRuntimeToken(ROOT, '--mw-font-editor-size', `${content}px`);
  setRuntimeToken(ROOT, '--mw-font-code-size', `${terminal}px`);

  updateRangeValue('set-ui-font-size-value', ui);
  updateRangeValue('set-content-font-size-value', content);
  updateRangeValue('set-terminal-font-size-value', terminal);
}

/**
 * Reset the font-size CSS variables to their defaults. Called by
 * 'reset all settings' so a reset doesn't leave stale inline values
 * on :root.
 */
function resetFontSizeCssVars(): void {
  clearRuntimeToken(ROOT, '--mw-font-ui-size');
  clearRuntimeToken(ROOT, '--mw-font-content-size');
  clearRuntimeToken(ROOT, '--mw-font-editor-size');
  clearRuntimeToken(ROOT, '--mw-font-code-size');
}

function applyFontFamily(fontFamily: Settings['fontFamily']): void {
  const override = resolveFontFamilyOverride(fontFamily);
  if (override === null) {
    clearRuntimeToken(ROOT, '--mw-font-sans');
    return;
  }
  setRuntimeToken(ROOT, '--mw-font-sans', override);
}

function resolveTheme(mode: Settings['themeMode']): 'light' | 'dark' {
  if (mode === 'system') {
    return systemThemeMedia?.matches ? 'dark' : 'light';
  }
  return mode;
}

function applyTheme(mode: Settings['themeMode']): void {
  const resolved = resolveTheme(mode);
  document.body.dataset.themeMode = mode;
  document.body.dataset.theme = resolved;
  ROOT.dataset.themeMode = mode;
  ROOT.dataset.theme = resolved;
  ROOT.style.colorScheme = resolved === 'light' ? 'light' : 'dark';
}

function applyAll(): void {
  applyFontSizes(current);
  applyFontFamily(current.fontFamily);
  applyTheme(current.themeMode);
  // 不在此处改 body.inspector-open：由 inspector.ts 在 bindInspectorUi()
  // 里根据 saved setting 自己 open/close，避免和按钮切换后的状态脱节。
}

function setActivePane(pane: string): void {
  if (settingsShell) {
    settingsShell.setActivePane(pane);
    return;
  }
  document.querySelectorAll<HTMLElement>('.set-v2-nav__item').forEach((el) => {
    el.classList.toggle('is-active', el.getAttribute('data-settings-pane') === pane);
  });
  document.querySelectorAll<HTMLElement>('.set-v2-pane').forEach((el) => {
    el.hidden = el.id !== `settings-pane-${pane}`;
  });
}

function mountSettingsShell(): void {
  if (settingsShell?.element.isConnected) return;
  settingsShell?.dispose();
  settingsShell = null;
  const staging = document.getElementById('settings-pane-staging');
  if (!staging) return;
  const panes = [...staging.querySelectorAll<HTMLElement>('.set-v2-pane')];
  settingsShell = createSettingsShell({
    panes,
    onPaneChange: onSettingsPaneOpened,
    onClose: closeSettingsModal,
  });
  staging.replaceWith(settingsShell.element);
}

function openSettingsModal(): void {
  mountSettingsShell();
  settingsShell?.setPaneVisible('work', productModeStore.get().productMode === 'work');
  settingsShell?.open();
  syncControlsToState();
  const pane = document.querySelector<HTMLElement>('.set-v2-nav__item.is-active')?.dataset.settingsPane;
  if (pane) onSettingsPaneOpened(pane);
  void syncAutoStartFromSystem();
  void syncCloseBehaviorFromMain();
  void syncStrictSecurityFromMain();
}

function closeSettingsModal(): void {
  settingsShell?.close();
  helpDocOverlay?.close();
  closeContactModal(false);
  $('#settings-btn')?.focus();
  disposeSettingsLibraryPane();
  // 停止 MCP 面板的 CUA 安装轮询，避免弹窗关闭后空跑
  disposeMcpPane();
}

function syncControlsToState(): void {
  // 主题
  const themeSel = document.querySelector<HTMLSelectElement>('#set-theme-mode');
  if (themeSel) themeSel.value = current.themeMode;
  const uiFontSizeRange = document.querySelector<HTMLInputElement>('#set-ui-font-size');
  if (uiFontSizeRange) uiFontSizeRange.value = String(current.uiFontSize);
  const contentFontSizeRange = document.querySelector<HTMLInputElement>('#set-content-font-size');
  if (contentFontSizeRange) contentFontSizeRange.value = String(current.contentFontSize);
  const terminalFontSizeRange = document.querySelector<HTMLInputElement>('#set-terminal-font-size');
  if (terminalFontSizeRange) terminalFontSizeRange.value = String(current.terminalFontSize);
  applyFontSizes(current);
  const fontFamilySel = document.querySelector<HTMLSelectElement>('#set-font-family');
  if (fontFamilySel) fontFamilySel.value = current.fontFamily;

  // 行为
  const closeSel = document.querySelector<HTMLSelectElement>('#set-close-behavior');
  if (closeSel) closeSel.value = current.closeBehavior;
  const autoStart = document.querySelector<HTMLInputElement>('#set-auto-start');
  if (autoStart) autoStart.checked = current.autoStart;
  const ins = document.querySelector<HTMLInputElement>('#set-inspector-open');
  if (ins) ins.checked = current.inspectorOpen;
}

function bindControls(): void {
  document.querySelector<HTMLSelectElement>('#set-theme-mode')?.addEventListener('change', (e) => {
    current.themeMode = (e.target as HTMLSelectElement).value as Settings['themeMode'];
    saveSettings(current);
    applyTheme(current.themeMode);
    const labels: Record<Settings['themeMode'], string> = {
      system: '跟随系统',
      light: '浅色',
      dark: '深色',
    };
    notify(`主题已切换：${labels[current.themeMode]}`);
  });

  bindFontRange('set-ui-font-size', 'uiFontSize');
  bindFontRange('set-content-font-size', 'contentFontSize');
  bindFontRange('set-terminal-font-size', 'terminalFontSize');

  document.querySelector<HTMLSelectElement>('#set-font-family')?.addEventListener('change', (e) => {
    current.fontFamily = (e.target as HTMLSelectElement).value as Settings['fontFamily'];
    saveSettings(current);
    applyFontFamily(current.fontFamily);
    notify('字体设置已更新（重启后部分应用生效）');
  });

  document.querySelector<HTMLSelectElement>('#set-close-behavior')?.addEventListener('change', (e) => {
    const select = e.target as HTMLSelectElement;
    const requested = select.value as Settings['closeBehavior'];
    void (async (): Promise<void> => {
      try {
        const result = await window.Crew?.setCloseBehavior?.(requested);
        const behavior = result?.closeBehavior;
        if (behavior !== 'tray' && behavior !== 'quit' && behavior !== 'ask') throw new Error('invalid close behavior');
        current.closeBehavior = behavior;
        select.value = behavior;
        saveSettings(current);
        notify('关闭行为已更新');
      } catch {
        select.value = current.closeBehavior;
        notify('关闭行为设置失败');
      }
    })();
  });
  document.querySelector<HTMLInputElement>('#set-auto-start')?.addEventListener('change', (e) => {
    const checkbox = e.target as HTMLInputElement;
    const requested = checkbox.checked;
    void (async (): Promise<void> => {
      try {
        const result = await window.Crew?.setAutoLaunchEnabled?.(requested);
        const enabled = result?.enabled ?? requested;
        current.autoStart = enabled;
        checkbox.checked = enabled;
        saveSettings(current);
        notify(enabled ? '已开启开机自启' : '已关闭开机自启');
      } catch {
        checkbox.checked = current.autoStart;
        notify('开机自启设置失败');
      }
    })();
  });
  document.querySelector<HTMLInputElement>('#set-strict-security')?.addEventListener('change', (e) => {
    const checkbox = e.target as HTMLInputElement;
    const previous = !checkbox.checked;
    void (async (): Promise<void> => {
      try {
        const result = await window.Crew?.setStrictSecurityEnabled?.(checkbox.checked);
        if (!result || typeof result.strictSecurityEnabled !== 'boolean') {
          throw new Error('invalid strict security preference');
        }
        checkbox.checked = result.strictSecurityEnabled;
        notify(result.strictSecurityEnabled ? '已开启严格安全约束，网关正在重启以应用' : '已启用兼容模式，网关正在重启以应用');
      } catch {
        checkbox.checked = previous;
        notify('安全策略设置失败');
      }
    })();
  });
  document.querySelector<HTMLInputElement>('#set-inspector-open')?.addEventListener('change', (e) => {
    current.inspectorOpen = (e.target as HTMLInputElement).checked;
    saveSettings(current);
    // 由 inspector.ts 监听这个事件，自己决定 open/close —— 单一所有权，
    // 避免和 toggle 按钮的状态脱节导致「保存是开、UI 却是关」之类的反直觉问题。
    window.dispatchEvent(new CustomEvent('inspector:setting-changed', { detail: { open: current.inspectorOpen } }));
  });

  // 数据 Tab 操作
  document.getElementById('set-clear-cache')?.addEventListener('click', () => {
    try {
      // 仅清缓存类 key，保留 settings/login
      Object.keys(localStorage)
        .filter((k) => k.startsWith('crew.cache.') || k.startsWith('crew.draft.'))
        .forEach((k) => localStorage.removeItem(k));
      notify('已清除本地缓存');
    } catch {
      notify('清除失败');
    }
  });

  document.getElementById('set-export-sessions')?.addEventListener('click', async () => {
    if (!requireRendererLogin()) return;
    const btn = document.getElementById('set-export-sessions') as HTMLButtonElement | null;
    if (btn) btn.disabled = true;
    try {
      const sessions = await backendApi.sessions(undefined, { includeArchived: true });
      const { exportSessionsWithMessages } = await import('../lib/session-export');
      await exportSessionsWithMessages(
        sessions.map((s) => ({ session_id: s.session_id, title: s.title })),
        (done, total) => {
          if (btn) btn.textContent = `导出中 ${done}/${total}`;
        },
      );
    } catch (e) {
      // 后端不通时导出本地缓存
      try {
        const localData: Array<{ id: string; title: string; workspaceId: string; updatedAt: string }> = [];
        Object.keys(localStorage)
          .filter((k) => k.startsWith('crew.session.') || k.startsWith('crew.workspace.'))
          .forEach((k) => {
            try {
              localData.push(JSON.parse(localStorage.getItem(k) || '{}'));
            } catch { /* ignore */ }
          });
        const blob = new Blob([JSON.stringify({ exportedAt: new Date().toISOString(), version: 'Crew v2.0.0 (local)', localData }, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `Crew-local-${new Date().toISOString().slice(0, 10)}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        notify('后端未连接，已导出本地数据');
      } catch {
        notify('导出失败：' + (e as Error).message);
      }
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = '导出';
      }
    }
  });

  document.getElementById('set-reset-all')?.addEventListener('click', () => {
    if (!window.confirm('确认重置全部设置？该操作不可撤销。')) return;
    current = { ...DEFAULT_SETTINGS };
    saveSettings(current);
    applyAll();
    // Ensure no stale inline font-size tokens survive a reset.
    // A reset removes the runtime override so tokens.css defaults apply again.
    resetFontSizeCssVars();
    applyFontSizes(current);
    syncControlsToState();
    notify('已重置为默认设置');
  });
}

function bindFontRange(id: string, key: 'uiFontSize' | 'contentFontSize' | 'terminalFontSize'): void {
  const range = document.querySelector<HTMLInputElement>(`#${id}`);
  range?.addEventListener('input', (e) => {
    current[key] = Number((e.target as HTMLInputElement).value);
    applyFontSizes(current);
  });
  range?.addEventListener('change', () => saveSettings(current));
}

function bindSettingsNav(): void {
  document.querySelectorAll<HTMLElement>('[data-settings-pane].set-v2-link-item--btn').forEach((el) => {
    el.addEventListener('click', () => {
      const pane = el.getAttribute('data-settings-pane');
      if (pane) {
        setActivePane(pane);
        onSettingsPaneOpened(pane);
      }
    });
  });
}

let configRenderers: {
  renderConfigModels: () => Promise<void> | void;
  renderPlatforms: () => Promise<void> | void;
} | null = null;

export function registerConfigPaneRenderers(r: {
  renderConfigModels: () => Promise<void> | void;
  renderPlatforms: () => Promise<void> | void;
}): void {
  configRenderers = r;
}

function onSettingsPaneOpened(pane: string): void {
  if (pane === 'account') {
    renderAuthAccount();
  }
  if (pane === 'model' && configRenderers) void configRenderers.renderConfigModels();
  if (pane === 'channel' && configRenderers) void configRenderers.renderPlatforms();
  if (pane === 'sys-logs') {
    void import('./system-page').then((m) => void m.renderSystemLogs());
  }
  if (pane === 'sys-usage') {
    void import('./usage-panel').then((m) => m.renderUsagePage());
  }
  if (pane === 'library') {
    void import('./settings-library').then((m) => void m.renderSettingsLibraryPane());
  }
  if (pane === 'mcp') {
    void import('./settings-mcp').then((m) => void m.bindMcpPane());
  }
  if (pane === 'work') {
    const workPane = document.getElementById('settings-pane-work');
    if (workPane) void import('./work/settings').then((module) => void module.renderWorkSettings(workPane));
  }
}

export function bindSettingsUi(): void {
  mountSettingsDataPanes();
  mountSettingsShell();
  $('#settings-btn')?.addEventListener('click', openSettingsModal);
  bindSettingsNav();
  bindSettingsLibraryUi();
  bindContactModal();
  window.addEventListener('pagehide', disposeSettingsLibraryPane, { once: true });
  bindSessionPreviewModal();
  bindControls();
  setActivePane('account');
  // 用户头像按钮：打开设置并切换到账户面板
  window.addEventListener('user:open-account', () => {
    openSettingsModal();
    setActivePane('account');
    renderAuthAccount();
  });
  // 启动时应用持久化的设置
  applyAll();
  void syncAutoStartFromSystem();
  void syncCloseBehaviorFromMain();
  void syncStrictSecurityFromMain();
  systemThemeMedia?.addEventListener('change', () => {
    if (current.themeMode === 'system') applyTheme('system');
  });

  // 关于页链接
  document.getElementById('set-view-changelog')?.addEventListener('click', () => {
    void window.Crew?.openExternal?.('https://github.com/shuishenghualalala/Ace/releases');
  });
  document.getElementById('set-link-official')?.addEventListener('click', (e) => {
    e.preventDefault();
    void window.Crew?.openExternal?.('https://github.com/shuishenghualalala/Ace');
  });
  document.getElementById('set-link-docs')?.addEventListener('click', (event) => {
    event.preventDefault();
    openHelpDoc(event.currentTarget as HTMLElement);
  });
  document.getElementById('set-link-contact')?.addEventListener('click', (event) => {
    event.preventDefault();
    openContactModal();
  });

}

export function getSettings(): Settings {
  return current;
}
