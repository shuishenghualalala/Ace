/** Desktop 灵感 App 图库、详情和网站元素批注工作台。 */

import {
  backendApi,
  type InspirationItem,
  type LocalSite,
  type SiteAnnotation,
} from '../backend-client';
import { $, escapeHtml, notify, state } from '../state';
import { showConfirmDialog } from '../ui-feedback';
import { closeInspector, enableInspectorSurfaceAutoWidth, openInspectorCustomView } from './inspector';
import { getSessionAgentDisplay } from './workspaces';
import { queryPrimaryComposer } from './composer-scope';

type Selection = {
  route: string; selector: string; element_tag: string; element_text: string;
  context: Record<string, unknown>;
};

type DraftAnnotation = Selection & { id: string; comment: string };
type AnnotationDraft = { site: LocalSite; annotations: DraftAnnotation[] };

let inspirations: InspirationItem[] = [];
let activeInspiration: InspirationItem | null = null;
let listPreviewTimer: ReturnType<typeof setTimeout> | null = null;
let workbenchSite: LocalSite | null = null;
let workbenchSelection: Selection | null = null;
let siteAnnotationMode = false;
let openInspirationAgent: ((item: InspirationItem) => Promise<void>) | null = null;
let createInspirationSession: (() => Promise<void>) | null = null;
let activeSessionSites: LocalSite[] = [];
let sessionSitesRefreshTimer: ReturnType<typeof setTimeout> | null = null;
const annotationDrafts = new Map<string, AnnotationDraft>();
const ANNOTATION_DRAFTS_KEY = 'ace.inspirationAnnotationDrafts.v1';
const LEGACY_ANNOTATION_DRAFTS_KEY = 'ace.siteAnnotationDrafts.v1';

function root(): HTMLElement | null { return $('#sites-page-root'); }
function previewUrl(item: Pick<InspirationItem, 'id'> | LocalSite): string {
  return `ace-site://${encodeURIComponent(item.id)}/`;
}

function saveDrafts(): void {
  localStorage.setItem(ANNOTATION_DRAFTS_KEY, JSON.stringify(Array.from(annotationDrafts.entries())));
}

function restoreDrafts(): void {
  try {
    const raw = localStorage.getItem(ANNOTATION_DRAFTS_KEY)
      || localStorage.getItem(LEGACY_ANNOTATION_DRAFTS_KEY) || '[]';
    const rows = JSON.parse(raw) as unknown;
    if (!Array.isArray(rows)) return;
    for (const row of rows) {
      if (!Array.isArray(row) || typeof row[0] !== 'string' || !row[1] || typeof row[1] !== 'object') continue;
      const draft = row[1] as AnnotationDraft;
      if (draft.site?.id && Array.isArray(draft.annotations)) annotationDrafts.set(row[0], draft);
    }
    saveDrafts();
    localStorage.removeItem(LEGACY_ANNOTATION_DRAFTS_KEY);
  } catch {
    localStorage.removeItem(ANNOTATION_DRAFTS_KEY);
  }
}

function formatUpdated(timestamp: number): string {
  const elapsed = Date.now() - timestamp * 1000;
  if (elapsed >= 0 && elapsed < 60_000) return '刚刚更新';
  if (elapsed >= 0 && elapsed < 3_600_000) return `${Math.max(1, Math.floor(elapsed / 60_000))} 分钟前更新`;
  if (elapsed >= 0 && elapsed < 86_400_000) return `${Math.max(1, Math.floor(elapsed / 3_600_000))} 小时前更新`;
  return new Date(timestamp * 1000).toLocaleDateString();
}

function inspirationCard(item: InspirationItem): string {
  return `<article class="inspiration-card" data-inspiration-card data-search="${escapeHtml(`${item.title} ${item.description}`.toLocaleLowerCase())}">
    <div class="inspiration-card__preview" role="button" tabindex="0" data-inspiration-id="${escapeHtml(item.id)}" aria-label="打开 ${escapeHtml(item.title)}">
      <iframe loading="lazy" src="${escapeHtml(previewUrl(item))}" tabindex="-1" aria-hidden="true" sandbox="allow-scripts allow-forms allow-modals allow-same-origin"></iframe>
      <span class="inspiration-card__open">打开</span>
    </div>
    <div class="inspiration-card__copy"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(formatUpdated(item.updatedAt))}</span></div>
    ${item.description ? `<p>${escapeHtml(item.description)}</p>` : ''}
  </article>`;
}

function renderGallery(el: HTMLElement): void {
  el.innerHTML = `<div class="sites-page inspiration-page">
    <header class="sites-head inspiration-head">
      <div><p class="sites-eyebrow">YOUR APPS</p><h1>灵感</h1><p>你和 Agent 一起设计、使用和继续完善的 App。</p></div>
      <div class="sites-head-actions"><button class="sites-refresh" data-inspirations-refresh aria-label="刷新灵感" title="刷新">↻</button><button class="sites-create" data-sites-create>＋ 新建灵感</button></div>
    </header>
    <label class="inspiration-search"><span aria-hidden="true">⌕</span><input type="search" data-inspiration-search placeholder="搜索灵感" autocomplete="off"></label>
    <main class="inspiration-gallery" data-inspiration-gallery>
      ${inspirations.map(inspirationCard).join('') || '<div class="inspiration-empty"><strong>把一个想法变成 App</strong><span>点击“新建灵感”，直接描述你想做什么。</span></div>'}
      <div class="inspiration-empty inspiration-empty--search" data-search-empty hidden><strong>没有匹配的灵感</strong><span>换个关键词试试。</span></div>
    </main>
  </div>`;
  bindGalleryEvents();
}

function renderDetail(el: HTMLElement, item: InspirationItem): void {
  el.innerHTML = `<div class="sites-page inspiration-page inspiration-detail">
    <header class="inspiration-detail__toolbar">
      <button class="sites-button" data-inspiration-back>‹ 灵感</button>
      <div><strong>${escapeHtml(item.title)}</strong>${item.description ? `<span>${escapeHtml(item.description)}</span>` : ''}</div>
      <span class="sites-toolbar-spacer"></span>
      <button class="sites-button" data-inspiration-modify>修改</button>
      <button class="sites-button" data-inspiration-share>分享</button>
      <button class="sites-button" data-inspiration-pin>固定到桌面</button>
      <button class="sites-button is-danger" data-inspiration-delete>删除</button>
    </header>
    <main class="inspiration-detail__stage"><div class="sites-frame-status" id="sites-preview-status">正在打开 App…</div><iframe id="sites-preview-frame" src="${escapeHtml(previewUrl(item))}" sandbox="allow-scripts allow-forms allow-modals allow-same-origin allow-downloads"></iframe></main>
  </div>`;
  bindDetailEvents(item);
}

function render(): void {
  const el = root();
  if (!el) return;
  if (activeInspiration) renderDetail(el, activeInspiration); else renderGallery(el);
}

async function loadInspirations(selectId = ''): Promise<void> {
  const result = await backendApi.inspirations();
  inspirations = result.inspirations;
  activeInspiration = selectId
    ? inspirations.find((item) => item.id === selectId) || null
    : activeInspiration ? inspirations.find((item) => item.id === activeInspiration?.id) || null : null;
  render();
}

function openSelected(id: string): void {
  activeInspiration = inspirations.find((item) => item.id === id) || null;
  render();
}

function bindGalleryEvents(): void {
  root()?.querySelector('[data-sites-create]')?.addEventListener('click', () => void createInspirationSession?.().catch(showError));
  root()?.querySelector('[data-inspirations-refresh]')?.addEventListener('click', () => void loadInspirations().catch(showError));
  root()?.querySelectorAll<HTMLElement>('[data-inspiration-id]').forEach((card) => {
    card.addEventListener('click', () => openSelected(card.dataset.inspirationId || ''));
    card.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openSelected(card.dataset.inspirationId || ''); }
    });
  });
  root()?.querySelector<HTMLInputElement>('[data-inspiration-search]')?.addEventListener('input', (event) => {
    const query = (event.currentTarget as HTMLInputElement).value.trim().toLocaleLowerCase();
    let visible = 0;
    root()?.querySelectorAll<HTMLElement>('[data-inspiration-card]').forEach((card) => {
      const matches = !query || (card.dataset.search || '').includes(query);
      card.hidden = !matches;
      if (matches) visible += 1;
    });
    const empty = root()?.querySelector<HTMLElement>('[data-search-empty]');
    if (empty) empty.hidden = visible > 0 || !query;
  });
}

function bindPreviewState(): void {
  if (listPreviewTimer) clearTimeout(listPreviewTimer);
  const frame = document.getElementById('sites-preview-frame') as HTMLIFrameElement | null;
  const status = document.getElementById('sites-preview-status');
  if (!frame || !status) return;
  frame.addEventListener('load', () => {
    if (listPreviewTimer) clearTimeout(listPreviewTimer);
    listPreviewTimer = null;
    status.hidden = true;
  }, { once: true });
  frame.addEventListener('error', () => {
    status.textContent = 'App 加载失败，请返回后重试。'; status.classList.add('is-error'); status.hidden = false;
  }, { once: true });
  listPreviewTimer = setTimeout(() => {
    status.textContent = 'App 加载时间较长，请检查生成内容。'; status.classList.add('is-error'); status.hidden = false;
  }, 15_000);
}

async function downloadShare(item: InspirationItem): Promise<void> {
  const prepared = await backendApi.exportInspiration(item.id);
  const save = window.Crew?.saveLocalExport;
  if (!save) throw new Error('当前 Desktop 版本不支持保存分享包');
  const result = await save(prepared.archive_path, prepared.filename);
  if (result.ok) notify('灵感分享包已保存');
}

function setPinButtonState(inspirationId: string, open: boolean): void {
  if (activeInspiration?.id !== inspirationId) return;
  const button = root()?.querySelector<HTMLButtonElement>('[data-inspiration-pin]');
  if (!button) return;
  button.textContent = open ? '取消固定' : '固定到桌面';
  button.title = open ? '关闭桌面便利贴' : '作为桌面便利贴打开';
  button.setAttribute('aria-pressed', String(open));
}

async function syncPinButton(item: InspirationItem): Promise<void> {
  const stateResult = await window.Crew?.inspirationWindowState?.(item.id);
  setPinButtonState(item.id, Boolean(stateResult?.open));
}

function bindDetailEvents(item: InspirationItem): void {
  bindPreviewState();
  void syncPinButton(item).catch(() => undefined);
  root()?.querySelector('[data-inspiration-back]')?.addEventListener('click', () => { activeInspiration = null; render(); });
  root()?.querySelector('[data-inspiration-modify]')?.addEventListener('click', () => void openInspirationAgent?.(item).catch(showError));
  root()?.querySelector('[data-inspiration-share]')?.addEventListener('click', () => void downloadShare(item).catch(showError));
  root()?.querySelector('[data-inspiration-pin]')?.addEventListener('click', () => void (async () => {
    const button = root()?.querySelector<HTMLButtonElement>('[data-inspiration-pin]');
    if (button) button.disabled = true;
    try {
      const current = await window.Crew?.inspirationWindowState?.(item.id);
      if (current?.open) {
        await window.Crew?.closeInspirationWindow?.(item.id);
        setPinButtonState(item.id, false);
      } else {
        await window.Crew?.openInspirationWindow?.(item.id, item.title);
        setPinButtonState(item.id, true);
      }
    } finally {
      if (button) button.disabled = false;
    }
  })().catch(showError));
  root()?.querySelector('[data-inspiration-delete]')?.addEventListener('click', () => void (async () => {
    const confirmed = await showConfirmDialog({
      title: '删除灵感', message: `确定删除“${item.title}”吗？原始工程和创建对话会保留。`, confirmText: '删除',
    });
    if (!confirmed) return;
    await window.Crew?.closeInspirationWindow?.(item.id);
    await backendApi.deleteInspiration(item.id);
    activeInspiration = null;
    await loadInspirations();
  })().catch(showError));
}

function renderAnnotationChip(): void {
  const box = document.getElementById('chat-site-annotation-preview');
  if (!box) return;
  const sessionId = state.activeSessionId || '';
  const draft = annotationDrafts.get(sessionId);
  const count = draft?.annotations.length || 0;
  box.replaceChildren(); box.hidden = count === 0;
  if (!draft || !count) return;
  const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'site-annotation-chip';
  chip.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 15a3 3 0 0 1-3 3H8l-5 4V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3Z"/></svg>';
  const label = document.createElement('span'); label.textContent = `${count} 条批注`; chip.appendChild(label);
  chip.addEventListener('click', () => mountSiteAnnotationWorkbench(draft.site));
  const clear = document.createElement('button'); clear.type = 'button'; clear.className = 'site-annotation-chip__clear'; clear.textContent = '×'; clear.setAttribute('aria-label', '清空页面批注');
  clear.addEventListener('click', () => { annotationDrafts.delete(sessionId); saveDrafts(); renderAnnotationChip(); });
  box.append(chip, clear);
}

function renderSessionAnnotationEntry(): void {
  const entry = document.getElementById('site-annotation-entry');
  const button = document.getElementById('site-annotation-button');
  const menu = document.getElementById('site-annotation-menu');
  if (!entry || !button || !menu) return;
  entry.hidden = activeSessionSites.length === 0 || state.activeTab !== 'chat';
  button.setAttribute('aria-expanded', 'false'); menu.hidden = true; menu.replaceChildren();
  if (activeSessionSites.length <= 1) return;
  for (const site of activeSessionSites) {
    const item = document.createElement('button'); item.type = 'button'; item.setAttribute('role', 'menuitem'); item.textContent = site.name;
    item.addEventListener('click', () => { menu.hidden = true; button.setAttribute('aria-expanded', 'false'); mountSiteAnnotationWorkbench(site); });
    menu.appendChild(item);
  }
}

async function syncSessionAnnotationEntry(): Promise<void> {
  const sessionId = state.activeSessionId || '';
  activeSessionSites = []; renderSessionAnnotationEntry();
  if (!sessionId) return;
  try {
    const result = await backendApi.sites(state.currentWorkspaceId || undefined);
    if (state.activeSessionId !== sessionId) return;
    activeSessionSites = result.sites.filter((site) => site.session_id === sessionId).sort((a, b) => b.updated_at - a.updated_at);
    renderSessionAnnotationEntry();
  } catch { activeSessionSites = []; renderSessionAnnotationEntry(); }
}

function showCommentEditor(selection: Selection): void {
  workbenchSelection = selection;
  const overlay = document.getElementById('site-annotation-overlay');
  if (!overlay) return;
  overlay.hidden = false;
  overlay.innerHTML = `<div class="site-comment-box"><code>${escapeHtml(selection.selector)}</code><textarea data-site-comment placeholder="描述你希望 Agent 如何修改…"></textarea><div><button data-site-comment-cancel>取消</button><button class="is-primary" data-site-comment-save>添加批注</button></div></div>`;
  const input = overlay.querySelector<HTMLTextAreaElement>('[data-site-comment]'); input?.focus();
  overlay.querySelector('[data-site-comment-cancel]')?.addEventListener('click', () => { workbenchSelection = null; overlay.hidden = true; });
  overlay.querySelector('[data-site-comment-save]')?.addEventListener('click', () => void addWorkbenchAnnotation(input?.value.trim() || '').catch(showError));
}

async function addWorkbenchAnnotation(comment: string): Promise<void> {
  const site = workbenchSite, selection = workbenchSelection, sessionId = state.activeSessionId || '';
  if (!site || !selection || !sessionId || !comment) return;
  const created = await backendApi.createInspirationAnnotation(site.id, {
    ...selection, comment, targetKind: 'site_dom',
  });
  const draft = annotationDrafts.get(sessionId) || { site, annotations: [] };
  draft.site = site; draft.annotations.push({ ...selection, id: created.annotation.id, comment });
  annotationDrafts.set(sessionId, draft); saveDrafts(); renderAnnotationChip(); workbenchSelection = null;
  const overlay = document.getElementById('site-annotation-overlay'); if (overlay) overlay.hidden = true;
}

function syncSiteWorkbenchMode(): void {
  const frame = document.getElementById('site-workbench-frame') as HTMLIFrameElement | null;
  frame?.contentWindow?.postMessage({ type: 'ace-site-annotation-mode', enabled: siteAnnotationMode }, '*');
  document.querySelectorAll<HTMLElement>('[data-site-surface-mode]').forEach((button) => {
    button.classList.toggle('is-active', button.dataset.siteSurfaceMode === (siteAnnotationMode ? 'annotate' : 'use'));
    if (button.dataset.siteSurfaceMode === 'annotate') button.textContent = siteAnnotationMode ? '完成选择' : '指出要修改的位置';
  });
  const label = document.querySelector<HTMLElement>('.site-workbench-title em');
  if (label) label.textContent = siteAnnotationMode ? '选择修改位置' : '预览';
}

function mountSiteSurface(site: LocalSite, annotate: boolean): void {
  if (!site.session_id || site.session_id !== state.activeSessionId) { notify('请先进入这个灵感绑定的对话'); return; }
  if (!openInspectorCustomView()) return;
  enableInspectorSurfaceAutoWidth();
  workbenchSite = site; workbenchSelection = null; siteAnnotationMode = annotate;
  const tabs = document.getElementById('chat-inspector-tabs'), body = document.getElementById('chat-inspector-body');
  if (!tabs || !body) return;
  const title = document.createElement('div'); title.className = 'site-workbench-title';
  const icon = document.createElement('span'); icon.ariaHidden = 'true'; icon.textContent = '◇';
  const name = document.createElement('strong'); name.textContent = site.name;
  const mode = document.createElement('em'); mode.textContent = annotate ? '选择修改位置' : '预览';
  title.append(icon, name, mode); tabs.replaceChildren(title);
  body.innerHTML = `<div class="site-annotation-workbench"><div class="site-surface-toolbar"><button type="button" data-site-surface-mode="annotate">指出要修改的位置</button></div><div class="site-workbench-frame-wrap"><div class="sites-frame-status" id="site-workbench-status">正在打开 App…</div><iframe id="site-workbench-frame" src="${escapeHtml(previewUrl(site))}" sandbox="allow-scripts allow-forms allow-modals allow-same-origin allow-downloads"></iframe><div id="site-annotation-overlay" class="site-annotation-overlay" hidden></div></div></div>`;
  document.body.classList.add('site-annotation-workbench-open');
  body.querySelectorAll<HTMLElement>('[data-site-surface-mode]').forEach((button) => button.addEventListener('click', () => {
    siteAnnotationMode = button.dataset.siteSurfaceMode === 'annotate';
    syncSiteWorkbenchMode();
  }));
  const frame = document.getElementById('site-workbench-frame') as HTMLIFrameElement | null;
  frame?.addEventListener('load', syncSiteWorkbenchMode);
  syncSiteWorkbenchMode();
  renderAnnotationChip();
}

export function mountSiteAnnotationWorkbench(site: LocalSite): void { mountSiteSurface(site, true); }

export function composeSiteAnnotationMessage(sessionId: string, text: string): string {
  const draft = annotationDrafts.get(sessionId);
  if (!draft?.annotations.length) return text.trim();
  const rows = draft.annotations.map((item, index) => `${index + 1}. 页面：${item.route}\n   元素：${item.selector}\n   标签：${item.element_tag}\n   当前文字：${item.element_text}\n   修改要求：${item.comment}\n   批注 ID：${item.id}`);
  return `${text.trim() || '请根据以下页面批注修改 App。'}\n\n灵感：${draft.site.name}\n源码目录：${draft.site.source_path}\n\n页面批注：\n${rows.join('\n\n')}\n\n修改完成后不要自动发布，等待我再次明确要求部署。`;
}

export function hasSiteAnnotationDraft(sessionId: string): boolean { return (annotationDrafts.get(sessionId)?.annotations.length || 0) > 0; }
export function clearSiteAnnotationDraft(sessionId: string): void { annotationDrafts.delete(sessionId); saveDrafts(); renderAnnotationChip(); }
export function buildSiteAnnotationPrompt(site: LocalSite, item: SiteAnnotation): string {
  return `请根据下面的灵感页面批注修改源码。修改完成后不要自动发布，等待我再次明确要求部署。\n\n灵感：${site.name}\n源码目录：${site.source_path}\n页面：${item.route}\n元素：${item.selector}\n标签：${item.element_tag}\n当前文字：${item.element_text}\n修改要求：${item.comment}\n批注 ID：${item.id}`;
}

function showError(error: unknown): void { notify(`灵感操作失败：${error instanceof Error ? error.message : String(error)}`); }

export function syncSiteComposerMarker(): void {
  const marker = document.getElementById('chat-sites-mode');
  const input = queryPrimaryComposer<HTMLTextAreaElement>('[data-composer-input]');
  const provider = getSessionAgentDisplay(state.activeSessionId)?.agentLabel?.provider || '';
  const isInspiration = provider.toLowerCase() === 'sites';
  if (marker) marker.hidden = !isInspiration;
  if (input) input.placeholder = isInspiration ? '描述你想创建或完善的 App…' : '输入消息...';
}

export function bindSitesTab(opts: {
  openInspirationAgent: (item: InspirationItem) => Promise<void>;
  createInspirationSession: () => Promise<void>;
}): void {
  openInspirationAgent = opts.openInspirationAgent;
  createInspirationSession = opts.createInspirationSession;
  restoreDrafts();
  window.Crew?.onInspirationWindowStateChanged?.(({ inspirationId, open }) => {
    setPinButtonState(inspirationId, open);
  });
  document.getElementById('site-annotation-button')?.addEventListener('click', () => {
    const button = document.getElementById('site-annotation-button'), menu = document.getElementById('site-annotation-menu');
    if (!button || !menu || !activeSessionSites.length) return;
    if (activeSessionSites.length === 1) { mountSiteAnnotationWorkbench(activeSessionSites[0]); return; }
    menu.hidden = !menu.hidden; button.setAttribute('aria-expanded', String(!menu.hidden));
  });
  document.addEventListener('click', (event) => {
    const entry = document.getElementById('site-annotation-entry');
    if (!entry || entry.contains(event.target as Node)) return;
    const menu = document.getElementById('site-annotation-menu'); if (menu) menu.hidden = true;
    document.getElementById('site-annotation-button')?.setAttribute('aria-expanded', 'false');
  });
  window.addEventListener('session:changed', () => {
    if (workbenchSite && workbenchSite.session_id !== state.activeSessionId) { workbenchSite = null; workbenchSelection = null; closeInspector(); }
    syncSiteComposerMarker(); renderAnnotationChip(); void syncSessionAnnotationEntry();
  });
  window.addEventListener('messages:changed', () => {
    if (sessionSitesRefreshTimer) clearTimeout(sessionSitesRefreshTimer);
    sessionSitesRefreshTimer = setTimeout(() => void syncSessionAnnotationEntry(), 1200);
  });
  window.addEventListener('inspiration:site-surface', (event) => {
    const detail = (event as CustomEvent<{ siteId?: string; sessionId?: string }>).detail;
    if (!detail?.siteId || detail.sessionId !== state.activeSessionId) return;
    void backendApi.site(detail.siteId).then((result) => mountSiteSurface(result.site, false)).catch(showError);
  });
  window.addEventListener('message', (event) => {
    if (event.data?.type === 'ace-widget-emit' && typeof event.data.widgetId === 'string') {
      void backendApi.emitWidget(event.data.widgetId, event.data.value).catch(showError); return;
    }
    if (event.data?.type === 'ace-widget-view-state' && typeof event.data.mountId === 'string' && typeof event.data.canvasId === 'string') {
      void backendApi.updateCanvasPlacement(event.data.canvasId, event.data.mountId, {
        viewState: event.data.value && typeof event.data.value === 'object' ? event.data.value : {},
      }).catch(showError); return;
    }
    const frame = document.getElementById('site-workbench-frame') as HTMLIFrameElement | null;
    if (frame && event.source === frame.contentWindow && event.data?.type === 'ace-site-preview-ready') {
      const status = document.getElementById('site-workbench-status'); if (status) status.hidden = true;
      syncSiteWorkbenchMode(); return;
    }
    if (frame && event.source === frame.contentWindow && event.data?.type === 'ace-site-preview-error') {
      const status = document.getElementById('site-workbench-status');
      if (status) { status.textContent = `App 加载失败：${String(event.data.message || '未知错误')}`; status.classList.add('is-error'); status.hidden = false; }
      return;
    }
    if (frame && event.source === frame.contentWindow && event.data?.type === 'ace-site-element-selected' && siteAnnotationMode) showCommentEditor(event.data.payload as Selection);
  });
  syncSiteComposerMarker(); renderAnnotationChip(); void syncSessionAnnotationEntry();
}

export function syncSiteAnnotationEntry(): void { void syncSessionAnnotationEntry(); }
export function renderSitesPage(): void { activeInspiration = null; void loadInspirations().catch(showError); }
