/**
 * 设置弹窗：侧栏齿轮按钮触发；内部"左 tab + 右表单"。
 *
 * 行为持久化：所有控件写入 localStorage `crew.settings`，实时应用主题/字体大小。
 * 真实行为：
 *   - 主题模式：写入 root/body theme 类与 data 属性，system 跟随 OS 深浅色变化
 *   - 强调色：覆盖 --v2-primary / --accent CSS 变量
 *   - 字体大小：覆盖 UI / 正文 / 编辑器 / 终端四组 CSS 变量
 *   - 关闭行为：仅记录偏好（实际生效需 Electron 主进程配合）
 *   - 清除缓存：扫描 localStorage 中 crew.* 命名的 key
 *   - 导出会话：读取后端 /api/sessions 并下载为 JSON
 */

import { backendApi } from '../backend-client';
import { renderMarkdownHtml } from '../markdown';
import helpDocMarkdown from '../../../assets/help-docs/crew-user-guide.md?raw';
import { bindSettingsLibraryUi } from './settings-library';
import { bindSessionPreviewModal } from './session-preview-modal';
import { $, notify } from '../state';
import {
  DEFAULT_SETTINGS,
  clampNumber,
  hydrateSettings,
  resolveFontFamily,
  type Settings,
} from './settings-preferences';

const STORAGE_KEY = 'crew.settings';
const ROOT = document.documentElement;
const HELP_DOC_ASSET_BASE = './help-docs/';
const HELP_DOC_VERSION_LABEL = `文档版本 ${__HELP_DOC_VERSION__}`;

type HelpDocHeading = {
  id: string;
  level: number;
  title: string;
  searchText: string;
};

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
let helpDocSource: string | null = null;
let helpDocHeadings: HelpDocHeading[] = [];
let helpDocLoading: Promise<void> | null = null;

function normalizeHelpDocTitle(line: string): string {
  return line
    .replace(/^#{1,6}\s+/, '')
    .replace(/\\([+_.()])/g, '$1')
    .replace(/\*\*(.*?)\*\*/g, '$1')
    .trim();
}

function parseHelpDocHeadings(source: string): HelpDocHeading[] {
  const headings: HelpDocHeading[] = [];
  source.split(/\r?\n/).forEach((line) => {
    const match = /^(#{1,4})\s+(.+)$/.exec(line);
    if (!match) return;
    const level = match[1].length;
    if (level > 3) return;
    const title = normalizeHelpDocTitle(line);
    headings.push({
      id: `help-doc-heading-${headings.length}`,
      level,
      title,
      searchText: title.toLowerCase(),
    });
  });
  return headings;
}

function rewriteHelpDocMarkdown(source: string): string {
  return source.replace(/!\[([^\]]*)\]\((image(?:-\d+)?\.png)\)/g, (_all, alt: string, filename: string) => {
    const safeAlt = alt || 'Crew 功能截图';
    return `![${safeAlt}](${HELP_DOC_ASSET_BASE}${filename})`;
  });
}

function assignHelpDocHeadingIds(body: HTMLElement): void {
  const renderedHeadings = Array.from(body.querySelectorAll<HTMLElement>('h1, h2, h3'));
  renderedHeadings.forEach((heading, index) => {
    const id = helpDocHeadings[index]?.id;
    if (!id) return;
    heading.id = id;
  });
}

function renderHelpDocTree(filter = ''): void {
  const tree = document.getElementById('help-doc-tree');
  const empty = document.getElementById('help-doc-empty');
  if (!tree || !empty) return;

  const query = filter.trim().toLowerCase();
  const visible = helpDocHeadings.filter((heading) => !query || heading.searchText.includes(query));
  tree.innerHTML = '';
  empty.hidden = visible.length > 0;

  visible.forEach((heading) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `help-doc-tree__item help-doc-tree__item--level-${heading.level}`;
    button.dataset.helpDocTarget = heading.id;
    button.textContent = heading.title;
    button.addEventListener('click', () => {
      const target = document.getElementById(heading.id);
      target?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    });
    tree.appendChild(button);
  });
}

function setHelpDocLoadingState(message: string): void {
  const body = document.getElementById('help-doc-content');
  if (body) {
    const state = document.createElement('div');
    state.className = 'help-doc-state';
    state.textContent = message;
    body.replaceChildren(state);
  }
}

async function loadHelpDoc(): Promise<void> {
  if (helpDocSource) return;
  if (helpDocLoading) return helpDocLoading;

  helpDocLoading = (async () => {
    setHelpDocLoadingState('正在加载帮助文档…');
    helpDocSource = helpDocMarkdown;
    helpDocHeadings = parseHelpDocHeadings(helpDocSource);

    const content = document.getElementById('help-doc-content');
    if (!content) return;
    content.innerHTML = renderMarkdownHtml(rewriteHelpDocMarkdown(helpDocSource), { allowImages: true });
    assignHelpDocHeadingIds(content);
    renderHelpDocTree((document.getElementById('help-doc-search') as HTMLInputElement | null)?.value || '');
  })();

  try {
    await helpDocLoading;
  } catch (error) {
    setHelpDocLoadingState(`帮助文档加载失败：${(error as Error).message || '未知错误'}`);
  } finally {
    helpDocLoading = null;
  }
}

function openHelpDocModal(): void {
  const modal = document.getElementById('help-doc-modal');
  if (!modal) return;
  modal.classList.add('show');
  void loadHelpDoc();
}

function closeHelpDocModal(): void {
  document.getElementById('help-doc-modal')?.classList.remove('show');
}

function bindHelpDocModal(): void {
  const version = document.getElementById('help-doc-version');
  if (version) version.textContent = HELP_DOC_VERSION_LABEL;
  document.getElementById('help-doc-modal-close')?.addEventListener('click', closeHelpDocModal);
  document.getElementById('help-doc-modal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeHelpDocModal();
  });
  document.getElementById('help-doc-search')?.addEventListener('input', (e) => {
    renderHelpDocTree((e.target as HTMLInputElement).value);
  });
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

function updateRangeValue(id: string, px: number): void {
  const el = document.getElementById(id);
  if (el) el.textContent = `${px}px`;
}

function applyFontSizes(s: Settings): void {
  const ui = clampNumber(s.uiFontSize, 12, 18, DEFAULT_SETTINGS.uiFontSize);
  const content = clampNumber(s.contentFontSize, 12, 20, DEFAULT_SETTINGS.contentFontSize);
  const editor = clampNumber(s.editorFontSize, 12, 20, DEFAULT_SETTINGS.editorFontSize);
  const terminal = clampNumber(s.terminalFontSize, 11, 18, DEFAULT_SETTINGS.terminalFontSize);

  current.uiFontSize = ui;
  current.contentFontSize = content;
  current.editorFontSize = editor;
  current.terminalFontSize = terminal;

  // Body font sizes are controlled by the CSS variable cascade so the UI
  // slider updates all scales derived from --base-font-size via calc().
  ROOT.style.setProperty('--base-font-size', `${ui}px`);
  ROOT.style.setProperty('--content-font-size', `${content}px`);
  ROOT.style.setProperty('--editor-font-size', `${editor}px`);
  ROOT.style.setProperty('--terminal-font-size', `${terminal}px`);

  updateRangeValue('set-ui-font-size-value', ui);
  updateRangeValue('set-content-font-size-value', content);
  updateRangeValue('set-editor-font-size-value', editor);
  updateRangeValue('set-terminal-font-size-value', terminal);
}

/**
 * Reset the 4 font-size CSS variables to their defaults. Called by
 * 'reset all settings' so a reset doesn't leave stale inline values
 * on :root.
 */
function resetFontSizeCssVars(): void {
  ROOT.style.removeProperty('--base-font-size');
  ROOT.style.removeProperty('--content-font-size');
  ROOT.style.removeProperty('--editor-font-size');
  ROOT.style.removeProperty('--terminal-font-size');
}

function applyAccent(name: Settings['accent']): void {
  const map: Record<Settings['accent'], string> = {
    blue: '#2563eb',
    indigo: '#4f46e5',
    violet: '#7c5cff',
    cyan: '#0891b2',
  };
  const color = map[name];
  // --color-accent is the source of truth. The remaining accent variables
  // are aliases maintained by the inline FOUC script and _tokens.css.
  ROOT.style.setProperty('--color-accent', color);
  ROOT.style.setProperty('--accent', color);
  ROOT.style.setProperty('--accent2', color);
  ROOT.style.setProperty('--v2-primary', color);
  document.body.style.setProperty('--mm-accent', color);
}

/**
 * Resolve the actual theme key for a given mode.
 *   system → light | dark (via matchMedia)
 *   light / dark / sepia / hc → as-is
 * 'sepia' and 'hc' are named themes that activate their own
 * :root[data-theme="..."] block in variables.css.
 */
function resolveTheme(mode: Settings['themeMode']): 'light' | 'dark' | 'sepia' | 'hc' {
  if (mode === 'system') {
    return systemThemeMedia?.matches ? 'dark' : 'light';
  }
  return mode;
}

function applyTheme(mode: Settings['themeMode']): void {
  const resolved = resolveTheme(mode);
  // Theme switching uses :root[data-theme]; each named theme
  // (light/dark/sepia/hc) defines its own block in variables.css.
  document.body.dataset.themeMode = mode;
  document.body.dataset.theme = resolved;
  ROOT.dataset.themeMode = mode;
  ROOT.dataset.theme = resolved;
  ROOT.style.colorScheme = resolved === 'sepia' || resolved === 'hc' || resolved === 'light' ? 'light' : 'dark';
  ROOT.style.setProperty('--sans', resolveFontFamily(current.fontFamily));
}

function applyAll(): void {
  applyFontSizes(current);
  applyAccent(current.accent);
  applyTheme(current.themeMode);
  // 不在此处改 body.inspector-open：由 inspector.ts 在 bindInspectorUi()
  // 里根据 saved setting 自己 open/close，避免和按钮切换后的状态脱节。
}

function setActivePane(pane: string): void {
  document.querySelectorAll<HTMLElement>('.set-v2-nav__item').forEach((el) => {
    el.classList.toggle('is-active', el.getAttribute('data-settings-pane') === pane);
  });
  document.querySelectorAll<HTMLElement>('.set-v2-pane').forEach((el) => {
    el.hidden = el.id !== `settings-pane-${pane}`;
  });
}

function openSettingsModal(): void {
  const modal = document.getElementById('settings-modal');
  if (!modal) return;
  modal.classList.add('show');
  // 清掉内联 display，交给 .modal-overlay.show { display:flex }，避免破坏垂直居中
  modal.style.removeProperty('display');
  syncControlsToState();
  void syncAutoStartFromSystem();
  void syncCloseBehaviorFromMain();
}

function closeSettingsModal(): void {
  const modal = document.getElementById('settings-modal');
  if (!modal) return;
  modal.classList.remove('show');
  modal.style.removeProperty('display');
  // 停止 MCP 面板的 CUA 安装轮询，避免弹窗关闭后空跑
  void import('./settings-mcp').then((m) => m.disposeMcpPane());
}

function syncControlsToState(): void {
  // 主题
  const themeSel = document.querySelector<HTMLSelectElement>('#set-theme-mode');
  if (themeSel) themeSel.value = current.themeMode;
  const accentSel = document.querySelector<HTMLSelectElement>('#set-accent');
  if (accentSel) accentSel.value = current.accent;
  const uiFontSizeRange = document.querySelector<HTMLInputElement>('#set-ui-font-size');
  if (uiFontSizeRange) uiFontSizeRange.value = String(current.uiFontSize);
  const contentFontSizeRange = document.querySelector<HTMLInputElement>('#set-content-font-size');
  if (contentFontSizeRange) contentFontSizeRange.value = String(current.contentFontSize);
  const editorFontSizeRange = document.querySelector<HTMLInputElement>('#set-editor-font-size');
  if (editorFontSizeRange) editorFontSizeRange.value = String(current.editorFontSize);
  const terminalFontSizeRange = document.querySelector<HTMLInputElement>('#set-terminal-font-size');
  if (terminalFontSizeRange) terminalFontSizeRange.value = String(current.terminalFontSize);
  applyFontSizes(current);
  const fontFamilySel = document.querySelector<HTMLSelectElement>('#set-font-family');
  if (fontFamilySel) fontFamilySel.value = current.fontFamily;

  // 行为
  const startConn = document.querySelector<HTMLInputElement>('#set-start-connect');
  if (startConn) startConn.checked = current.startWithConnect;
  const closeSel = document.querySelector<HTMLSelectElement>('#set-close-behavior');
  if (closeSel) closeSel.value = current.closeBehavior;
  const autoStart = document.querySelector<HTMLInputElement>('#set-auto-start');
  if (autoStart) autoStart.checked = current.autoStart;
  const enterToSend = document.querySelector<HTMLInputElement>('#set-enter-send');
  if (enterToSend) enterToSend.checked = current.enterToSend;
  const streaming = document.querySelector<HTMLInputElement>('#set-streaming');
  if (streaming) streaming.checked = current.streaming;
  const ins = document.querySelector<HTMLInputElement>('#set-inspector-open');
  if (ins) ins.checked = current.inspectorOpen;
}

function bindControls(): void {
  document.querySelector<HTMLSelectElement>('#set-theme-mode')?.addEventListener('change', (e) => {
    current.themeMode = (e.target as HTMLSelectElement).value as Settings['themeMode'];
    saveSettings(current);
    applyTheme(current.themeMode);
    notify(`主题已切换：${current.themeMode === 'system' ? '跟随系统' : current.themeMode === 'dark' ? '深色' : '浅色'}`);
  });

  document.querySelector<HTMLSelectElement>('#set-accent')?.addEventListener('change', (e) => {
    current.accent = (e.target as HTMLSelectElement).value as Settings['accent'];
    saveSettings(current);
    applyAccent(current.accent);
    notify('强调色已更新');
  });

  bindFontRange('set-ui-font-size', 'uiFontSize');
  bindFontRange('set-content-font-size', 'contentFontSize');
  bindFontRange('set-editor-font-size', 'editorFontSize');
  bindFontRange('set-terminal-font-size', 'terminalFontSize');

  document.querySelector<HTMLSelectElement>('#set-font-family')?.addEventListener('change', (e) => {
    current.fontFamily = (e.target as HTMLSelectElement).value as Settings['fontFamily'];
    saveSettings(current);
    ROOT.style.setProperty('--sans', resolveFontFamily(current.fontFamily));
    notify('字体设置已更新（重启后部分应用生效）');
  });

  document.querySelector<HTMLInputElement>('#set-start-connect')?.addEventListener('change', (e) => {
    current.startWithConnect = (e.target as HTMLInputElement).checked;
    saveSettings(current);
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
  document.querySelector<HTMLInputElement>('#set-enter-send')?.addEventListener('change', (e) => {
    current.enterToSend = (e.target as HTMLInputElement).checked;
    saveSettings(current);
  });
  document.querySelector<HTMLInputElement>('#set-streaming')?.addEventListener('change', (e) => {
    current.streaming = (e.target as HTMLInputElement).checked;
    saveSettings(current);
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
    // Remove inline font-size variables so variables.css defaults take effect.
    resetFontSizeCssVars();
    applyFontSizes(current);
    syncControlsToState();
    notify('已重置为默认设置');
  });
}

function bindFontRange(id: string, key: 'uiFontSize' | 'contentFontSize' | 'editorFontSize' | 'terminalFontSize'): void {
  const range = document.querySelector<HTMLInputElement>(`#${id}`);
  range?.addEventListener('input', (e) => {
    current[key] = Number((e.target as HTMLInputElement).value);
    applyFontSizes(current);
  });
  range?.addEventListener('change', () => saveSettings(current));
}

function bindSettingsNav(): void {
  document.querySelectorAll<HTMLElement>('.set-v2-nav__item').forEach((el) => {
    el.addEventListener('click', () => {
      const pane = el.getAttribute('data-settings-pane');
      if (pane) {
        setActivePane(pane);
        onSettingsPaneOpened(pane);
      }
    });
  });
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
}

export function bindSettingsUi(): void {
  $('#settings-btn')?.addEventListener('click', openSettingsModal);
  document.getElementById('settings-modal-close')?.addEventListener('click', closeSettingsModal);
  document.getElementById('settings-modal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeSettingsModal();
  });
  bindSettingsNav();
  bindSettingsLibraryUi();
  bindSessionPreviewModal();
  bindHelpDocModal();
  bindControls();
  setActivePane('general');
  window.addEventListener('settings:open-projects', () => openProjectsPane());
  // 启动时应用持久化的设置
  applyAll();
  void syncAutoStartFromSystem();
  void syncCloseBehaviorFromMain();
  systemThemeMedia?.addEventListener('change', () => {
    if (current.themeMode === 'system') applyTheme('system');
  });

  document.getElementById('set-link-docs')?.addEventListener('click', (e) => {
    e.preventDefault();
    openHelpDocModal();
  });

}

export function getSettings(): Settings {
  return current;
}

function openProjectsPane(): void {
  openSettingsModal();
  setActivePane('projects');
  onSettingsPaneOpened('projects');
}
