import {
  backendApi,
  type BlueprintCanvas,
  type BlueprintWidget,
  type CanvasPlacement,
  type ChatChunk,
} from '../backend-client';
import { escapeHtml, state } from '../state';
import {
  disableInspectorSurfaceAutoWidth,
  enableInspectorSurfaceAutoWidth,
  openInspectorCustomView,
} from './inspector';

export interface BlueprintSurface {
  kind: 'inspiration';
  mode: 'widget' | 'canvas';
  sessionId: string;
  widgetId?: string;
  canvasId?: string;
  title: string;
  resourceRevision?: number;
  status?: 'preparing' | 'ready';
}

interface ElementSelection {
  route: string;
  selector: string;
  element_tag: string;
  element_text: string;
}

interface BlueprintAnnotation extends ElementSelection {
  id: string;
  comment: string;
  targetKind: 'canvas' | 'widget' | 'widget_dom';
  widgetId: string;
  widgetTitle: string;
  workspacePath: string;
  canvasId: string;
  canvasTitle: string;
  mountId: string;
  resourceRevision: number;
}

const SURFACES_KEY = 'ace.blueprintSurfaces.v1';
const ANNOTATIONS_KEY = 'ace.blueprintAnnotations.v1';
const surfaces = new Map<string, BlueprintSurface>();
const annotations = new Map<string, BlueprintAnnotation[]>();
let activeSurface: BlueprintSurface | null = null;
let activeWidgets: Record<string, BlueprintWidget> = {};
let activeCanvas: BlueprintCanvas | null = null;
let annotationMode = false;
let selected: {
  selection: ElementSelection;
  targetKind: 'canvas' | 'widget' | 'widget_dom';
  widgetId: string;
  canvasId: string;
  mountId: string;
} | null = null;
let pollTimer: ReturnType<typeof setTimeout> | null = null;
let pollGeneration = 0;

function persist<T>(key: string, value: Map<string, T>): void {
  localStorage.setItem(key, JSON.stringify(Array.from(value.entries())));
}

function restoreMap<T>(key: string): Map<string, T> {
  try {
    const parsed = JSON.parse(localStorage.getItem(key) || '[]') as unknown;
    return new Map(Array.isArray(parsed) ? parsed.filter((row): row is [string, T] =>
      Array.isArray(row) && typeof row[0] === 'string' && row[1] !== null && typeof row[1] === 'object') : []);
  } catch {
    localStorage.removeItem(key);
    return new Map();
  }
}

function widgetUrl(widgetId: string, mountId = '', revision = 0): string {
  const query = new URLSearchParams();
  if (mountId) query.set('mount_id', mountId);
  query.set('revision', String(revision));
  return `ace-site://${encodeURIComponent(widgetId)}/?${query.toString()}`;
}

function validSurface(value: unknown, fallbackSessionId: string): BlueprintSurface | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  if (!['blueprint', 'inspiration'].includes(String(item.kind || ''))
    || (item.mode !== 'widget' && item.mode !== 'canvas')) return null;
  const widgetId = typeof item.widgetId === 'string' ? item.widgetId : '';
  const canvasId = typeof item.canvasId === 'string' ? item.canvasId : '';
  if (item.mode === 'widget' && !widgetId.startsWith('widget_')) return null;
  if (item.mode === 'canvas' && !canvasId.startsWith('canvas_')) return null;
  return {
    kind: 'inspiration', mode: item.mode,
    sessionId: typeof item.sessionId === 'string' && item.sessionId ? item.sessionId : fallbackSessionId,
    title: typeof item.title === 'string' && item.title ? item.title : item.mode === 'widget' ? 'Widget' : 'Canvas',
    ...(widgetId ? { widgetId } : {}), ...(canvasId ? { canvasId } : {}),
    ...(typeof item.resourceRevision === 'number' ? { resourceRevision: item.resourceRevision } : {}),
    ...(item.status === 'ready' || item.status === 'preparing' ? { status: item.status } : {}),
  };
}

export function handleBlueprintSurfaceToolChunk(chunk: ChatChunk, sessionId: string): void {
  if (chunk.kind !== 'tool') return;
  const body = chunk.body as { phase?: string; name?: string; detail?: string };
  if (body.phase !== 'result' || !['Widget', 'Canvas', 'publish_site'].includes(String(body.name || '')) || !body.detail) return;
  try {
    const payload = JSON.parse(body.detail) as { surface?: unknown };
    const raw = payload.surface as Record<string, unknown> | undefined;
    if (raw?.kind === 'inspiration' && raw.mode === 'site') {
      // Website results are opened from the persisted Inspiration card. Do not
      // steal focus from the conversation when the tool result arrives.
      return;
    }
    const surface = validSurface(payload.surface, sessionId);
    if (!surface || surface.sessionId !== sessionId) return;
    surfaces.set(sessionId, surface);
    persist(SURFACES_KEY, surfaces);
    // Keep the result available to the conversation card; the user opens the
    // preview explicitly from that card.
  } catch {
    // Ordinary tool results are not Surface commands.
  }
}

function workbenchElements(): { tabs: HTMLElement; body: HTMLElement } | null {
  const tabs = document.getElementById('chat-inspector-tabs');
  const body = document.getElementById('chat-inspector-body');
  return tabs && body ? { tabs, body } : null;
}

function renderShell(surface: BlueprintSurface): HTMLElement | null {
  if (!openInspectorCustomView()) return null;
  enableInspectorSurfaceAutoWidth();
  const elements = workbenchElements();
  if (!elements) return null;
  document.body.classList.remove('site-annotation-workbench-open');
  document.body.classList.add('blueprint-surface-open');
  const title = document.createElement('div'); title.className = 'blueprint-surface-title';
  const icon = document.createElement('span'); icon.ariaHidden = 'true'; icon.textContent = '◇';
  const name = document.createElement('strong'); name.textContent = surface.title;
  const mode = document.createElement('em'); mode.textContent = surface.status === 'preparing' ? '正在制作' : '预览';
  title.append(icon, name, mode); elements.tabs.replaceChildren(title);
  elements.body.innerHTML = `<div class="blueprint-surface">
    <div class="blueprint-surface-toolbar"><span data-blueprint-status>正在准备预览…</span><button type="button" data-blueprint-annotate>指出要修改的位置</button></div>
    <div class="blueprint-surface-stage" data-blueprint-stage><div class="blueprint-surface-placeholder">正在制作界面…</div></div>
    <div class="site-annotation-overlay" data-blueprint-overlay hidden></div>
  </div>`;
  elements.body.querySelector('[data-blueprint-annotate]')?.addEventListener('click', () => {
    annotationMode = !annotationMode;
    syncAnnotationMode();
  });
  return elements.body;
}

function widgetReady(widget: BlueprintWidget): boolean {
  return widget.validation?.status === 'valid';
}

function widgetFrame(widget: BlueprintWidget, placement?: CanvasPlacement): HTMLElement {
  if (!widgetReady(widget)) {
    const placeholder = document.createElement('div'); placeholder.className = 'blueprint-surface-placeholder';
    const title = document.createElement('strong'); title.textContent = widget.title;
    const detail = document.createElement('span'); detail.textContent = '正在制作组件…';
    placeholder.append(title, detail); return placeholder;
  }
  const frame = document.createElement('iframe');
  frame.dataset.blueprintWidgetId = widget.id;
  frame.dataset.blueprintMountId = placement?.mountId || '';
  frame.src = widgetUrl(widget.id, placement?.mountId, widget.resourceRevision);
  frame.setAttribute('sandbox', 'allow-scripts allow-forms allow-modals allow-same-origin');
  return frame;
}

function renderWidgetSurface(widget: BlueprintWidget): void {
  const stage = document.querySelector<HTMLElement>('[data-blueprint-stage]');
  const status = document.querySelector<HTMLElement>('[data-blueprint-status]');
  if (!stage || !status) return;
  stage.className = 'blueprint-surface-stage is-widget';
  stage.replaceChildren(widgetFrame(widget));
  status.textContent = widgetReady(widget) ? '预览已更新' : '正在制作组件…';
  bindFrames();
}

function renderCanvasSurface(canvas: BlueprintCanvas, widgets: Record<string, BlueprintWidget>): void {
  const stage = document.querySelector<HTMLElement>('[data-blueprint-stage]');
  const status = document.querySelector<HTMLElement>('[data-blueprint-status]');
  if (!stage || !status) return;
  stage.className = 'blueprint-surface-stage is-canvas';
  const placements = canvas.placements || [];
  const singleWidget = placements.length === 1;
  const grid = document.createElement('div');
  grid.className = `blueprint-surface-grid${singleWidget ? ' is-single-widget' : ''}`;
  placements.forEach((placement) => {
    const widget = widgets[placement.widgetId];
    if (!widget) return;
    const layout = placement.layout;
    const section = document.createElement('section');
    section.className = `blueprint-surface-widget${singleWidget ? ' is-single-widget' : ''}`;
    section.dataset.blueprintCanvasId = canvas.id;
    section.dataset.blueprintWidgetTarget = widget.id;
    section.dataset.blueprintMountTarget = placement.mountId;
    if (!singleWidget) {
      section.style.gridColumn = `${layout.x + 1}/span ${layout.w}`;
      section.style.gridRow = `${layout.y + 1}/span ${layout.h}`;
    }
    const header = document.createElement('header'); header.textContent = widget.title;
    section.append(header, widgetFrame(widget, placement)); grid.appendChild(section);
  });
  if (!grid.childElementCount) {
    const placeholder = document.createElement('div'); placeholder.className = 'blueprint-surface-placeholder';
    placeholder.textContent = '正在整理内容…'; grid.appendChild(placeholder);
  }
  stage.replaceChildren(grid);
  grid.addEventListener('click', (event) => {
    if (!annotationMode || !(event.target instanceof Element)) return;
    const section = event.target.closest<HTMLElement>('[data-blueprint-widget-target]');
    if (!section) return;
    event.preventDefault(); event.stopPropagation();
    const widget = widgets[section.dataset.blueprintWidgetTarget || ''];
    if (!widget) return;
    selected = {
      targetKind: 'widget', widgetId: widget.id, canvasId: canvas.id,
      mountId: section.dataset.blueprintMountTarget || '',
      selection: {
        route: '/', selector: `[data-widget-id="${widget.id}"]`,
        element_tag: 'widget', element_text: widget.title,
      },
    };
    showCommentEditor();
  });
  status.textContent = `${placements.length} 个内容 · 正在同步预览`;
  bindFrames();
}

function bindFrames(): void {
  document.querySelectorAll<HTMLIFrameElement>('[data-blueprint-widget-id]').forEach((frame) => {
    frame.addEventListener('load', () => {
      frame.contentWindow?.postMessage({ type: 'ace-blueprint-annotation-mode', enabled: annotationMode }, '*');
    });
  });
}

function syncAnnotationMode(): void {
  const button = document.querySelector<HTMLButtonElement>('[data-blueprint-annotate]');
  button?.classList.toggle('is-active', annotationMode);
  if (button) button.textContent = annotationMode ? '完成选择' : '指出要修改的位置';
  document.querySelectorAll<HTMLIFrameElement>('[data-blueprint-widget-id]').forEach((frame) =>
    frame.contentWindow?.postMessage({ type: 'ace-blueprint-annotation-mode', enabled: annotationMode }, '*'));
}

function selectSurfaceTarget(): void {
  if (!activeSurface) return;
  if (activeSurface.mode === 'canvas' && activeCanvas) {
    selected = {
      targetKind: 'canvas', widgetId: '', canvasId: activeCanvas.id, mountId: '',
      selection: {
        route: '/', selector: ':canvas', element_tag: 'canvas',
        element_text: activeCanvas.title,
      },
    };
  } else {
    const widgetId = activeSurface.widgetId || '';
    const widget = activeWidgets[widgetId];
    if (!widget) return;
    selected = {
      targetKind: 'widget', widgetId, canvasId: activeSurface.canvasId || '', mountId: '',
      selection: {
        route: '/', selector: ':widget', element_tag: 'widget',
        element_text: widget.title,
      },
    };
  }
  showCommentEditor();
}

function surfaceSignature(): string {
  if (activeSurface?.mode === 'widget') {
    const widget = activeSurface.widgetId ? activeWidgets[activeSurface.widgetId] : undefined;
    return widget ? `${widget.id}:${widget.resourceRevision}:${widget.validation?.status}` : '';
  }
  return JSON.stringify({
    placements: activeCanvas?.placements?.map((item) => [item.mountId, item.updatedAt]),
    widgets: Object.values(activeWidgets).map((item) => [item.id, item.resourceRevision, item.validation?.status]),
  });
}

async function refreshSurface(force = false): Promise<void> {
  const surface = activeSurface;
  if (!surface || surface.sessionId !== state.activeSessionId) return;
  const before = surfaceSignature();
  if (surface.mode === 'widget' && surface.widgetId) {
    const result = await backendApi.widget(surface.widgetId);
    activeWidgets = { [result.widget.id]: result.widget };
    if (force || before !== surfaceSignature()) renderWidgetSurface(result.widget);
  } else if (surface.mode === 'canvas' && surface.canvasId) {
    const result = await backendApi.canvas(surface.canvasId);
    activeCanvas = result.canvas; activeWidgets = result.widgets;
    if (force || before !== surfaceSignature()) renderCanvasSurface(result.canvas, result.widgets);
  }
}

function schedulePoll(generation: number): void {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(() => {
    if (generation !== pollGeneration || activeSurface?.sessionId !== state.activeSessionId
      || !document.body.classList.contains('inspector-open')
      || !document.body.classList.contains('blueprint-surface-open')) return;
    void refreshSurface().catch((error) => {
      const status = document.querySelector<HTMLElement>('[data-blueprint-status]');
      if (status) status.textContent = `预览暂不可用，正在重试：${error instanceof Error ? error.message : String(error)}`;
    }).finally(() => schedulePoll(generation));
  }, 1000);
}

export async function mountBlueprintSurface(surface: BlueprintSurface): Promise<void> {
  if (surface.sessionId !== state.activeSessionId) return;
  activeSurface = surface; activeWidgets = {}; activeCanvas = null; annotationMode = false; selected = null;
  if (!renderShell(surface)) return;
  const generation = ++pollGeneration;
  await refreshSurface(true).catch((error) => {
    const status = document.querySelector<HTMLElement>('[data-blueprint-status]');
    if (status) status.textContent = `预览暂不可用：${error instanceof Error ? error.message : String(error)}`;
  });
  schedulePoll(generation);
  renderAnnotationChip();
}

function frameForSource(source: MessageEventSource | null): HTMLIFrameElement | null {
  return Array.from(document.querySelectorAll<HTMLIFrameElement>('[data-blueprint-widget-id]'))
    .find((frame) => frame.contentWindow === source) || null;
}

function showCommentEditor(): void {
  if (!selected) return;
  const overlay = document.querySelector<HTMLElement>('[data-blueprint-overlay]');
  if (!overlay) return;
  overlay.hidden = false;
  overlay.innerHTML = `<div class="site-comment-box"><code>${escapeHtml(selected.selection.selector)}</code><textarea data-blueprint-comment placeholder="描述你希望 Agent 如何修改…"></textarea><div><button data-blueprint-cancel>取消</button><button class="is-primary" data-blueprint-save>添加批注</button></div></div>`;
  const input = overlay.querySelector<HTMLTextAreaElement>('[data-blueprint-comment]'); input?.focus();
  overlay.querySelector('[data-blueprint-cancel]')?.addEventListener('click', () => { selected = null; overlay.hidden = true; });
  overlay.querySelector('[data-blueprint-save]')?.addEventListener('click', () => {
    void addAnnotation(input?.value.trim() || '').catch((error) => {
      const status = document.querySelector<HTMLElement>('[data-blueprint-status]');
      if (status) status.textContent = `批注保存失败：${error instanceof Error ? error.message : String(error)}`;
    });
  });
}

async function addAnnotation(comment: string): Promise<void> {
  const surface = activeSurface, target = selected;
  if (!surface || !target || !comment) return;
  const widget = target.widgetId ? activeWidgets[target.widgetId] : undefined;
  if (target.targetKind !== 'canvas' && !widget) return;
  const rows = annotations.get(surface.sessionId) || [];
  const context = {
    targetKind: target.targetKind,
    widgetId: widget?.id || '', widgetTitle: widget?.title || '', workspacePath: widget?.workspacePath || '',
    canvasId: target.canvasId, canvasTitle: activeCanvas?.title || '', mountId: target.mountId,
    resourceRevision: widget?.resourceRevision || 0,
  };
  const annotationOwnerId = target.canvasId || widget?.id || '';
  const annotationId = annotationOwnerId
    ? (await backendApi.createInspirationAnnotation(annotationOwnerId, {
      ...target.selection, comment, revisionId: String(widget?.resourceRevision || activeCanvas?.updatedAt || 0),
      targetKind: target.targetKind, canvasId: target.canvasId, widgetId: widget?.id || '',
      mountId: target.mountId, context,
    })).annotation.id
    : `blueprint_note_${Date.now()}`;
  rows.push({
    ...target.selection, id: annotationId, comment, targetKind: target.targetKind,
    widgetId: widget?.id || '', widgetTitle: widget?.title || '', workspacePath: widget?.workspacePath || '',
    canvasId: target.canvasId, canvasTitle: activeCanvas?.title || '', mountId: target.mountId,
    resourceRevision: widget?.resourceRevision || 0,
  });
  annotations.set(surface.sessionId, rows); persist(ANNOTATIONS_KEY, annotations); renderAnnotationChip();
  selected = null;
  const overlay = document.querySelector<HTMLElement>('[data-blueprint-overlay]'); if (overlay) overlay.hidden = true;
}

function renderAnnotationChip(): void {
  const root = document.getElementById('chat-blueprint-annotation-preview');
  if (!root) return;
  const sessionId = state.activeSessionId || '', rows = annotations.get(sessionId) || [];
  root.replaceChildren(); root.hidden = rows.length === 0;
  if (!rows.length) return;
  const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'site-annotation-chip';
  chip.textContent = `${rows.length} 条 App 批注`;
  chip.addEventListener('click', () => { const surface = surfaces.get(sessionId); if (surface) void mountBlueprintSurface(surface); });
  const clear = document.createElement('button'); clear.type = 'button'; clear.className = 'site-annotation-chip__clear'; clear.textContent = '×';
  clear.addEventListener('click', () => clearBlueprintAnnotationDraft(sessionId));
  root.append(chip, clear);
}

export function hasBlueprintAnnotationDraft(sessionId: string): boolean {
  return (annotations.get(sessionId)?.length || 0) > 0;
}

export function composeBlueprintAnnotationMessage(sessionId: string, text: string): string {
  const rows = annotations.get(sessionId) || [];
  if (!rows.length) return text.trim();
  const details = rows.map((item, index) => `${index + 1}. 目标：${item.targetKind}\n   Widget：${item.widgetTitle || '无'}${item.widgetId ? ` (${item.widgetId})` : ''}\n   源码：${item.workspacePath ? `${item.workspacePath}/index.html` : 'Canvas 布局'}\n   Canvas：${item.canvasTitle || '单组件预览'}${item.mountId ? ` / mount ${item.mountId}` : ''}\n   Revision：${item.resourceRevision}\n   元素：${item.selector}\n   标签：${item.element_tag}\n   当前文字：${item.element_text}\n   修改要求：${item.comment}`).join('\n\n');
  return `${text.trim() || '请根据以下预览批注修改 App。'}\n\n灵感生成物批注：\n${details}\n\n请修改原 Widget 文件，完成后调用 Widget.validate；不要新建替代 Widget。`;
}

export function clearBlueprintAnnotationDraft(sessionId: string): void {
  annotations.delete(sessionId); persist(ANNOTATIONS_KEY, annotations); renderAnnotationChip();
}

export function bindBlueprintSurface(): void {
  for (const [key, value] of restoreMap<BlueprintSurface>(SURFACES_KEY)) {
    const surface = validSurface(value, key);
    if (surface && surface.sessionId === key) surfaces.set(key, surface);
  }
  for (const [key, value] of restoreMap<BlueprintAnnotation[]>(ANNOTATIONS_KEY)) {
    if (Array.isArray(value)) annotations.set(key, value);
  }
  window.addEventListener('session:changed', () => {
    pollGeneration += 1;
    document.body.classList.remove('blueprint-surface-open');
    disableInspectorSurfaceAutoWidth();
    renderAnnotationChip();
  });
  window.addEventListener('message', (event) => {
    if (event.data?.type === 'ace-widget-view-state' && activeCanvas && typeof event.data.mountId === 'string') {
      void backendApi.updateCanvasPlacement(activeCanvas.id, event.data.mountId, {
        viewState: event.data.value && typeof event.data.value === 'object' ? event.data.value : {},
      });
      return;
    }
    if (event.data?.type !== 'ace-blueprint-element-selected' || !annotationMode) return;
    const frame = frameForSource(event.source);
    if (!frame || !activeSurface) return;
    selected = {
      targetKind: 'widget_dom',
      selection: event.data.payload as ElementSelection,
      widgetId: String(event.data.widgetId || frame.dataset.blueprintWidgetId || ''),
      canvasId: String(event.data.canvasId || activeSurface.canvasId || ''),
      mountId: String(event.data.mountId || frame.dataset.blueprintMountId || ''),
    };
    showCommentEditor();
  });
  renderAnnotationChip();
}
