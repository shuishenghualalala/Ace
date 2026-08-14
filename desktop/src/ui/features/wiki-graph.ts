/**
 * Wiki 知识图谱视图（Phase 3）——wiki-page 的第四种列表视图「图谱」。
 *
 * 数据：GET /api/wiki/graph（backendApi.wikiGraph，全量节点 + 关系边，不走分页）。
 * 布局：力导向模拟在 Web Worker 中计算（src/ui/wiki-graph-layout.worker.ts，
 *       esbuild 独立产物 dist/assets/wiki-graph-layout.worker.js）；无 Worker 环境
 *       （happy-dom 单测 / Worker 创建失败）回退主线程同步调用同一纯函数。
 * 渲染：SVG（viewBox 1600×1100）；节点按 page_type 着色、
 *       source_of 边带箭头；标签用原生 <text>（截断 + <title> 全文），不用
 *       foreignObject（happy-dom 兼容与转义都更稳）。
 * 交互（对齐 web WikiGraphView）：滚轮缩放、拖拽平移、双击空白适应画布、
 *       右下角缩放条（+ / − / 适应 / 1:1）、「显示来源节点」开关、点击页面节点选中。
 *
 * 解耦：不 import wiki-page；wiki-page 通过 mountWikiGraph 注入容器 + 回调，
 * 本模块自管数据 / 布局 / 视口状态（wiki-page 每次 renderShell 重挂载时状态不丢）。
 */

import { backendApi, type WikiGraph } from '../backend-client';
import { escapeHtml } from '../state';
import {
  computeWikiGraphLayout,
  estimateGraphTextWidth,
  type WikiGraphLayoutInput,
  type WikiGraphLayoutNode,
  type WikiGraphLayoutOutput,
} from '../wiki-graph-layout.worker';

export interface WikiGraphCallbacks {
  /** 点击页面节点（source 节点不可点）。 */
  onSelectPage: (pageId: string) => void;
  /** 当前选中页面 id（渲染选中高亮环）。 */
  getSelectedId: () => string | null;
}

interface WikiGraphTransform {
  x: number;
  y: number;
  scale: number;
}

interface WikiGraphViewState {
  /** 当前数据所属 KB；mount 时 kbId 不同则重置并重新拉取。 */
  kbId: string | null;
  graph: WikiGraph | null;
  loading: boolean;
  error: string | null;
  showSources: boolean;
  hiddenSources: number;
  transform: WikiGraphTransform;
  layout: WikiGraphLayoutOutput | null;
  computing: boolean;
  /** 布局完成后自动适应一次画布（新数据 / 过滤变化后置位）。 */
  pendingFit: boolean;
}

const CANVAS_WIDTH = 1600;
const CANVAS_HEIGHT = 1100;
const MIN_SCALE = 0.25;
const MAX_SCALE = 5;

function initialGraphState(): WikiGraphViewState {
  return {
    kbId: null,
    graph: null,
    loading: false,
    error: null,
    showSources: true,
    hiddenSources: 0,
    transform: { x: 0, y: 0, scale: 1 },
    layout: null,
    computing: false,
    pendingFit: false,
  };
}

let graphState: WikiGraphViewState = initialGraphState();

/** 挂载点与回调（wiki-page 每次 renderShell 后重新 mount 更新）。 */
let mountRef: { container: HTMLElement; callbacks: WikiGraphCallbacks } | null = null;

/** 数据加载代际：切 KB / 刷新后作废旧请求回包。 */
let loadSeq = 0;
/** 布局计算代际：过滤条件变化后作废旧计算结果。 */
let layoutSeq = 0;

// ── 布局 Worker（懒加载单例；失败回退主线程同步计算） ──

let layoutWorker: Worker | null = null;
let workerUnavailable = false;
const pendingLayouts: Array<{
  input: WikiGraphLayoutInput;
  resolve: (output: WikiGraphLayoutOutput) => void;
}> = [];

function ensureLayoutWorker(): Worker | null {
  if (workerUnavailable) return null;
  if (layoutWorker) return layoutWorker;
  if (typeof Worker !== 'function') {
    workerUnavailable = true;
    return null;
  }
  try {
    const worker = new Worker('./wiki-graph-layout.worker.js');
    // Worker 串行处理请求，响应顺序与请求一致，按队列逐个兑现。
    worker.onmessage = (e: MessageEvent<WikiGraphLayoutOutput>) => {
      pendingLayouts.shift()?.resolve(e.data);
    };
    worker.onerror = () => {
      workerUnavailable = true;
      layoutWorker?.terminate();
      layoutWorker = null;
      // 运行期失败：队列中的请求回退主线程同步计算，不丢任务。
      const queued = pendingLayouts.splice(0);
      for (const item of queued) item.resolve(computeWikiGraphLayout(item.input));
    };
    layoutWorker = worker;
    return worker;
  } catch {
    workerUnavailable = true;
    return null;
  }
}

function runLayout(input: WikiGraphLayoutInput): Promise<WikiGraphLayoutOutput> {
  const worker = ensureLayoutWorker();
  if (!worker) {
    // 无 Worker（happy-dom 单测）或创建失败：同一纯函数同步计算。
    return Promise.resolve().then(() => computeWikiGraphLayout(input));
  }
  return new Promise((resolve) => {
    pendingLayouts.push({ input, resolve });
    worker.postMessage(input);
  });
}

// ── 数据加载 / 布局 ──

async function loadGraph(kbId: string): Promise<void> {
  const seq = ++loadSeq;
  graphState.loading = true;
  graphState.error = null;
  graphState.graph = null;
  graphState.layout = null;
  rerender();
  try {
    const res = await backendApi.wikiGraph(kbId);
    if (seq !== loadSeq) return;
    graphState.graph = res.graph ?? { nodes: [], edges: [] };
    graphState.loading = false;
    graphState.pendingFit = true;
    startLayout();
  } catch (err) {
    if (seq !== loadSeq) return;
    graphState.loading = false;
    graphState.error = (err as Error).message;
    graphState.graph = null;
    graphState.layout = null;
    rerender();
  }
}

function startLayout(): void {
  const graph = graphState.graph;
  if (!graph || graph.nodes.length === 0) {
    graphState.layout = null;
    graphState.computing = false;
    rerender();
    return;
  }
  const filtered = graphState.showSources
    ? graph.nodes
    : graph.nodes.filter((n) => n.type !== 'source');
  graphState.hiddenSources = graph.nodes.length - filtered.length;
  const input: WikiGraphLayoutInput = {
    nodes: filtered.map((n) => ({
      id: n.id,
      title: n.title,
      type: (n.type === 'concept' ? 'entity' : n.type) as 'entity' | 'topic' | 'source' | 'comparison' | 'synthesis',
    })),
    edges: graph.edges,
    width: CANVAS_WIDTH,
    height: CANVAS_HEIGHT,
  };
  const seq = ++layoutSeq;
  graphState.computing = true;
  rerender();
  void runLayout(input).then((output) => {
    if (seq !== layoutSeq) return;
    graphState.layout = output;
    graphState.computing = false;
    rerender();
  });
}

// ── 视口变换（缩放 / 平移 / 适应；只改属性，不重渲染） ──

function graphSvg(): SVGSVGElement | null {
  return mountRef?.container.querySelector<SVGSVGElement>('[data-graph-svg]') ?? null;
}

/** svg 实际显示尺寸换算画布坐标的比例；happy-dom 下 rect 全 0，按 1:1 处理。 */
function scaleBaseOf(svg: SVGSVGElement): number {
  const rect = svg.getBoundingClientRect();
  return rect.width > 0 ? CANVAS_WIDTH / rect.width : 1;
}

function applyTransform(): void {
  const viewport = mountRef?.container.querySelector('[data-graph-viewport]');
  const { x, y, scale } = graphState.transform;
  viewport?.setAttribute('transform', `translate(${x}, ${y}) scale(${scale})`);
  const stats = mountRef?.container.querySelector('.wiki-graph__stats');
  if (stats) stats.textContent = statsText();
}

function zoomBy(factor: number, centerX?: number, centerY?: number): void {
  const prev = graphState.transform;
  const nextScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, prev.scale * factor));
  const cx = centerX ?? CANVAS_WIDTH / 2;
  const cy = centerY ?? CANVAS_HEIGHT / 2;
  graphState.transform = {
    scale: nextScale,
    x: prev.x + cx * (prev.scale - nextScale),
    y: prev.y + cy * (prev.scale - nextScale),
  };
  applyTransform();
}

/** 适应画布：让全部节点在可视区留 20px 边距（对齐 web handleFit）。 */
function fitToCanvas(): void {
  const svg = graphSvg();
  const layout = graphState.layout;
  if (!svg || !layout || layout.nodes.length === 0) {
    graphState.transform = { x: 0, y: 0, scale: 1 };
    applyTransform();
    return;
  }
  const rect = svg.getBoundingClientRect();
  const viewW = rect.width > 0 ? rect.width : CANVAS_WIDTH;
  const viewH = rect.height > 0 ? rect.height : CANVAS_HEIGHT;
  const xs = layout.nodes.map((n) => n.x - n.width / 2);
  const xe = layout.nodes.map((n) => n.x + n.width / 2);
  const ys = layout.nodes.map((n) => n.y - n.height / 2);
  const ye = layout.nodes.map((n) => n.y + n.height / 2);
  const minX = Math.min(...xs) - 40;
  const maxX = Math.max(...xe) + 40;
  const minY = Math.min(...ys) - 40;
  const maxY = Math.max(...ye) + 40;
  const contentW = maxX - minX;
  const contentH = maxY - minY;
  const scaleX = ((viewW - 40) / contentW) * (CANVAS_WIDTH / viewW);
  const scaleY = ((viewH - 40) / contentH) * (CANVAS_HEIGHT / viewH);
  const scale = Math.min(scaleX, scaleY, 1.5);
  graphState.transform = {
    scale,
    x: 20 * (CANVAS_WIDTH / viewW) - minX * scale,
    y: 20 * (CANVAS_HEIGHT / viewH) - minY * scale,
  };
  applyTransform();
}

// ── 渲染 ──

function statsText(): string {
  if (!graphState.graph || graphState.graph.nodes.length === 0) return '';
  const layout = graphState.layout;
  if (!layout) return '';
  const parts = [`${layout.nodes.length} 节点`, `${layout.edges.length} 关系`];
  if (graphState.hiddenSources > 0) parts.push(`${graphState.hiddenSources} 来源已隐藏`);
  parts.push(`${Math.round(graphState.transform.scale * 100)}%`);
  return parts.join(' · ');
}

/** 按节点宽度截断标题（CJK 宽字符按 15px 估算），全文放 <title>。 */
function truncateLabel(title: string, nodeWidth: number): string {
  const maxWidth = nodeWidth - 24;
  if (estimateGraphTextWidth(title) <= maxWidth) return title;
  let out = '';
  for (const ch of title) {
    if (estimateGraphTextWidth(out + ch + '…') > maxWidth) break;
    out += ch;
  }
  return `${out}…`;
}

function nodeHtml(
  node: WikiGraphLayoutNode,
  selectedId: string | null,
): string {
  const type = node.type || 'topic';
  const visualType = type === 'comparison' || type === 'synthesis' ? 'topic' : type;
  const classes = ['wiki-graph-node', `wiki-graph-node--${escapeHtml(visualType)}`];
  if (type === 'source') classes.push('wiki-graph-node--source');
  const active = node.id === selectedId;
  if (active) classes.push('wiki-graph-node--active');
  const ring = active
    ? `<rect class="wiki-graph-node__ring" x="-4" y="-4" width="${node.width + 8}" height="${node.height + 8}" rx="14" ry="14"></rect>`
    : '';
  return `
    <g class="${classes.join(' ')}" transform="translate(${node.x - node.width / 2}, ${node.y - node.height / 2})" data-node-id="${escapeHtml(node.id)}" data-node-type="${escapeHtml(type)}">
      ${ring}
      <rect class="wiki-graph-node__rect" width="${node.width}" height="${node.height}" rx="12" ry="12"></rect>
      <text class="wiki-graph-node__label" x="${node.width / 2}" y="${node.height / 2}" text-anchor="middle" dominant-baseline="central">${escapeHtml(truncateLabel(node.title, node.width))}</text>
      <title>${escapeHtml(node.title)}</title>
    </g>`;
}

function svgHtml(): string {
  const layout = graphState.layout;
  if (!layout) return '';
  const nodeById = new Map(layout.nodes.map((n) => [n.id, n]));
  const selectedId = mountRef?.callbacks.getSelectedId() ?? null;
  const edges = layout.edges
    .map((edge) => {
      const s = nodeById.get(edge.source);
      const t = nodeById.get(edge.target);
      if (!s || !t) return '';
      const arrow = edge.relation === 'source_of' ? ' marker-end="url(#wiki-graph-arrowhead)"' : '';
      return `<line class="wiki-graph-edge wiki-graph-edge--${escapeHtml(edge.relation)}" x1="${s.x}" y1="${s.y}" x2="${t.x}" y2="${t.y}"${arrow}></line>`;
    })
    .join('');
  const nodes = layout.nodes.map((n) => nodeHtml(n, selectedId)).join('');
  const { x, y, scale } = graphState.transform;
  return `
    <svg class="wiki-graph__svg" viewBox="0 0 ${CANVAS_WIDTH} ${CANVAS_HEIGHT}" preserveAspectRatio="xMidYMid meet" data-graph-svg>
      <defs>
        <marker id="wiki-graph-arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
          <polygon points="0 0, 10 3.5, 0 7"></polygon>
        </marker>
      </defs>
      <g data-graph-viewport transform="translate(${x}, ${y}) scale(${scale})">
        ${edges}
        ${nodes}
      </g>
    </svg>`;
}

function overlayHtml(hasGraph: boolean): string {
  if (graphState.loading) {
    return '<div class="wiki-graph__overlay">加载图谱中…</div>';
  }
  if (graphState.error) {
    return `
      <div class="wiki-graph__overlay">
        加载图谱失败：${escapeHtml(graphState.error)}
        <button type="button" class="wiki-graph__retry" data-graph-retry>重试</button>
      </div>`;
  }
  if (!hasGraph) {
    return '<div class="wiki-graph__overlay">当前知识库没有页面，无法生成图谱。</div>';
  }
  if (graphState.computing) {
    return '<div class="wiki-graph__overlay">计算布局中…</div>';
  }
  return '';
}

function graphHtml(): string {
  const hasGraph = !graphState.loading && !graphState.error && !!graphState.graph && graphState.graph.nodes.length > 0;
  const showSvg = hasGraph && !graphState.computing;
  return `
    <div class="wiki-graph">
      <div class="wiki-graph__toolbar">
        <button type="button" class="wiki-graph__btn" data-graph-refresh title="刷新图谱">刷新</button>
        <label class="wiki-graph__toggle" title="隐藏来源节点可减少杂乱">
          <input type="checkbox" data-graph-toggle-sources${graphState.showSources ? ' checked' : ''}>
          <span>显示来源节点</span>
        </label>
        <span class="wiki-graph__stats">${escapeHtml(statsText())}</span>
      </div>
      <div class="wiki-graph__canvas">
        ${overlayHtml(hasGraph)}
        ${showSvg ? svgHtml() : ''}
        ${hasGraph ? `
          <div class="wiki-graph__zoombar">
            <button type="button" data-graph-zoom-in title="放大">+</button>
            <button type="button" data-graph-zoom-out title="缩小">−</button>
            <button type="button" data-graph-fit title="适应画布">适应</button>
            <button type="button" data-graph-reset title="重置">1:1</button>
          </div>` : ''}
      </div>
      <div class="wiki-graph__hint">滚轮缩放，拖拽平移，双击空白处自适应。节点按类型着色，来源节点可隐藏。</div>
    </div>`;
}

// ── 事件绑定 ──

function bindGraphEvents(container: HTMLElement): void {
  const reload = (): void => {
    if (graphState.kbId) void loadGraph(graphState.kbId);
  };
  container.querySelector('[data-graph-refresh]')?.addEventListener('click', reload);
  container.querySelector('[data-graph-retry]')?.addEventListener('click', reload);

  const toggle = container.querySelector<HTMLInputElement>('[data-graph-toggle-sources]');
  toggle?.addEventListener('change', () => {
    if (toggle.checked === graphState.showSources) return;
    graphState.showSources = toggle.checked;
    graphState.pendingFit = true;
    startLayout();
  });

  container.querySelector('[data-graph-zoom-in]')?.addEventListener('click', () => zoomBy(1.2));
  container.querySelector('[data-graph-zoom-out]')?.addEventListener('click', () => zoomBy(0.83));
  container.querySelector('[data-graph-fit]')?.addEventListener('click', () => fitToCanvas());
  container.querySelector('[data-graph-reset]')?.addEventListener('click', () => {
    graphState.transform = { x: 0, y: 0, scale: 1 };
    applyTransform();
  });

  const svg = container.querySelector<SVGSVGElement>('[data-graph-svg]');
  if (svg) {
    let pan: { startX: number; startY: number; tx: number; ty: number } | null = null;
    svg.addEventListener(
      'wheel',
      (e: WheelEvent) => {
        e.preventDefault();
        const rect = svg.getBoundingClientRect();
        const base = scaleBaseOf(svg);
        const factor = e.deltaY < 0 ? 1.15 : 0.87;
        zoomBy(factor, (e.clientX - rect.left) * base, (e.clientY - rect.top) * base);
      },
      { passive: false },
    );
    svg.addEventListener('mousedown', (e: MouseEvent) => {
      // 仅在点击画布空白处（target 为 svg 本身）时开始平移
      if (e.target !== svg) return;
      e.preventDefault();
      pan = { startX: e.clientX, startY: e.clientY, tx: graphState.transform.x, ty: graphState.transform.y };
      svg.classList.add('wiki-graph__svg--panning');
    });
    svg.addEventListener('mousemove', (e: MouseEvent) => {
      if (!pan) return;
      const base = scaleBaseOf(svg);
      const dx = (e.clientX - pan.startX) * base;
      const dy = (e.clientY - pan.startY) * base;
      graphState.transform = { ...graphState.transform, x: pan.tx + dx, y: pan.ty + dy };
      applyTransform();
    });
    const endPan = (): void => {
      pan = null;
      svg.classList.remove('wiki-graph__svg--panning');
    };
    svg.addEventListener('mouseup', endPan);
    svg.addEventListener('mouseleave', endPan);
    svg.addEventListener('dblclick', (e: MouseEvent) => {
      if (e.target !== svg) return;
      fitToCanvas();
    });
  }

  container.querySelectorAll('[data-node-id]').forEach((el) => {
    el.addEventListener('click', () => {
      // source 节点不是 Wiki 页面，不可选中（对齐 web pageById 判断）。
      if (el.getAttribute('data-node-type') === 'source') return;
      const id = el.getAttribute('data-node-id') ?? '';
      if (id) mountRef?.callbacks.onSelectPage(id);
    });
  });
}

/**
 * 上次渲染签名：wiki-page 的 renderShell 会保留画布节点并重新 mount，
 * 容器 / 数据 / 布局 / 选中态均未变时跳过全量 SVG 重建与逐节点事件重绑。
 */
interface GraphRenderSig {
  container: HTMLElement;
  graph: WikiGraph | null;
  layout: WikiGraphLayoutOutput | null;
  loading: boolean;
  computing: boolean;
  error: string | null;
  showSources: boolean;
  selectedId: string | null;
}

let lastRenderSig: GraphRenderSig | null = null;

function rerender(): void {
  if (!mountRef || !mountRef.container.isConnected) return;
  const sig: GraphRenderSig = {
    container: mountRef.container,
    graph: graphState.graph,
    layout: graphState.layout,
    loading: graphState.loading,
    computing: graphState.computing,
    error: graphState.error,
    showSources: graphState.showSources,
    selectedId: mountRef.callbacks.getSelectedId() ?? null,
  };
  const prev = lastRenderSig;
  if (
    prev &&
    prev.container === sig.container &&
    prev.graph === sig.graph &&
    prev.layout === sig.layout &&
    prev.loading === sig.loading &&
    prev.computing === sig.computing &&
    prev.error === sig.error &&
    prev.showSources === sig.showSources &&
    prev.selectedId === sig.selectedId
  ) {
    return;
  }
  lastRenderSig = sig;
  mountRef.container.innerHTML = graphHtml();
  bindGraphEvents(mountRef.container);
  if (graphState.pendingFit && !graphState.computing && graphState.layout && graphState.layout.nodes.length > 0) {
    graphState.pendingFit = false;
    fitToCanvas();
  }
}

// ── 对外入口 ──

/**
 * 挂载 / 重挂载图谱视图。wiki-page 每次 renderShell 后在 graph 视图下调用；
 * 数据 / 布局 / 视口状态保存在本模块，重挂载不丢（不重新拉取，除非 KB 变化）。
 */
export function mountWikiGraph(container: HTMLElement, kbId: string, callbacks: WikiGraphCallbacks): void {
  mountRef = { container, callbacks };
  if (graphState.kbId !== kbId) {
    loadSeq += 1;
    layoutSeq += 1;
    graphState = { ...initialGraphState(), kbId };
    void loadGraph(kbId);
    return;
  }
  rerender();
}

/** 让当前 KB 的图谱数据失效（整页刷新 / 登录态变化时调用），下次 mount 重新拉取。 */
export function invalidateWikiGraph(): void {
  loadSeq += 1;
  layoutSeq += 1;
  graphState = initialGraphState();
  lastRenderSig = null;
}

/** 测试钩子：重置模块状态（含 Worker 单例）。 */
export function __resetWikiGraphForTest(): void {
  invalidateWikiGraph();
  layoutWorker?.terminate();
  layoutWorker = null;
  workerUnavailable = false;
  pendingLayouts.length = 0;
  mountRef = null;
}
