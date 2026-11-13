/**
 * @vitest-environment happy-dom
 *
 * Wiki 图谱视图单测（Phase 3）。
 * 覆盖：布局纯函数（computeWikiGraphLayout，worker 壳不测）、图谱数据加载渲染、
 *       节点点击选中 + 右栏详情、隐藏来源开关、滚轮缩放 / 拖拽平移 / 重置、
 *       加载失败重试与空态。
 * mock 方式与 wiki-page.test.ts 一致（vi.mock backend-client + happy-dom 容器）；
 * happy-dom 无 Worker，wiki-graph 自动回退主线程同步布局（同一纯函数）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi, type WikiGraph, type WikiPage } from '../../src/ui/backend-client';

// 与 wiki-page.test.ts 同理：整页 innerHTML 重渲染 + waitFor 轮询，并行高负载下放宽超时。
vi.setConfig({ testTimeout: 20000 });
import { __resetWikiViewForTest, refreshWikiData } from '../../src/ui/features/wiki-page';
import {
  computeWikiGraphLayout,
  estimateGraphTextWidth,
} from '../../src/ui/wiki-graph-layout.worker';

vi.mock('../../src/ui/backend-client', () => ({
  backendApi: {
    wikiKBs: vi.fn(),
    wikiInit: vi.fn(),
    wikiPages: vi.fn(),
    wikiPage: vi.fn(),
    wikiGraph: vi.fn(),
    wikiUpload: vi.fn(),
    wikiIngest: vi.fn(),
    wikiCancelIngest: vi.fn(),
    wikiDeletePage: vi.fn(),
    wikiDeletePages: vi.fn(),
  },
}));

vi.mock('../../src/ui/ui-feedback', () => ({
  showConfirmDialog: vi.fn(async () => true),
}));

const api = backendApi as unknown as {
  wikiKBs: ReturnType<typeof vi.fn>;
  wikiInit: ReturnType<typeof vi.fn>;
  wikiPages: ReturnType<typeof vi.fn>;
  wikiPage: ReturnType<typeof vi.fn>;
  wikiGraph: ReturnType<typeof vi.fn>;
  wikiUpload: ReturnType<typeof vi.fn>;
  wikiIngest: ReturnType<typeof vi.fn>;
  wikiCancelIngest: ReturnType<typeof vi.fn>;
  wikiDeletePage: ReturnType<typeof vi.fn>;
  wikiDeletePages: ReturnType<typeof vi.fn>;
};

const NOW = Math.floor(Date.now() / 1000);

function makePage(partial: Partial<WikiPage>): WikiPage {
  return {
    id: 'p1',
    page_type: 'entity',
    title: '页面',
    file_path: 'dir/a.md',
    sources: [],
    related: [],
    tags: [],
    created_at: NOW - 86400,
    updated_at: NOW,
    aliases: [],
    ...partial,
  };
}

function makeGraph(): WikiGraph {
  return {
    nodes: [
      { id: 'p1', title: 'React 笔记', type: 'entity' },
      { id: 'p2', title: 'Vue 笔记', type: 'entity' },
      { id: 's1', title: 'report.pdf', type: 'source' },
    ],
    edges: [
      { source: 'p1', target: 'p2', relation: 'related' },
      { source: 's1', target: 'p1', relation: 'source_of' },
    ],
  };
}

const root = () => document.querySelector('#wiki-page-root') as HTMLElement;
const flush = () => new Promise((r) => setTimeout(r, 0));

/** 进入图谱视图并等待布局完成（svg 出现）。 */
async function openGraphView(): Promise<void> {
  await refreshWikiData();
  root().querySelector('[data-wiki-view="graph"]')?.dispatchEvent(new Event('click'));
  await vi.waitFor(() => expect(root().querySelector('.wiki-graph__svg')).not.toBeNull(), {
    timeout: 10000,
    interval: 20,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  __resetWikiViewForTest();
  document.body.innerHTML = `
    <button class="nav-item" data-tab="wiki">Wiki</button>
    <section id="wiki-tab" class="tab-pane"><div id="wiki-page-root"></div></section>
  `;
  api.wikiKBs.mockResolvedValue({
    ok: true,
    kbs: [{ id: 'default', name: '默认知识库', created_at: 0, updated_at: 0 }],
  });
  api.wikiInit.mockResolvedValue({ ok: true });
  api.wikiPages.mockResolvedValue({
    ok: true,
    pages: [makePage({ id: 'p1', title: 'React 笔记' }), makePage({ id: 'p2', title: 'Vue 笔记', page_type: 'entity' })],
    source_titles: {},
    source_files: {},
  });
  api.wikiPage.mockResolvedValue({
    ok: true,
    page: makePage({ id: 'p1', title: 'React 笔记', content: '**正文**' }),
    source_titles: {},
    source_files: {},
  });
  api.wikiGraph.mockResolvedValue({ ok: true, graph: makeGraph() });
});

// ── 布局纯函数（worker 文件导出；worker 壳不测） ──

describe('computeWikiGraphLayout 纯函数', () => {
  it('节点落在画布边界内，source 节点更矮', () => {
    const out = computeWikiGraphLayout({
      nodes: [
        { id: 'a', title: '节点 A', type: 'entity' },
        { id: 'b', title: '节点 B', type: 'topic' },
        { id: 's', title: '来源.pdf', type: 'source' },
      ],
      edges: [{ source: 'a', target: 'b', relation: 'related' }],
      width: 1600,
      height: 1100,
    });
    expect(out.nodes.length).toBe(3);
    for (const n of out.nodes) {
      expect(n.x).toBeGreaterThanOrEqual(n.width / 2 + 14 - 0.001);
      expect(n.x).toBeLessThanOrEqual(1600 - n.width / 2 - 14 + 0.001);
      expect(n.y).toBeGreaterThanOrEqual(n.height / 2 + 14 - 0.001);
      expect(n.y).toBeLessThanOrEqual(1100 - n.height / 2 - 14 + 0.001);
    }
    expect(out.nodes.find((n) => n.id === 's')?.height).toBe(32);
    expect(out.nodes.find((n) => n.id === 'a')?.height).toBe(52);
    // 相连的 a/b 计入 degree，孤立的 s 为 0
    expect(out.nodes.find((n) => n.id === 'a')?.degree).toBe(1);
    expect(out.nodes.find((n) => n.id === 's')?.degree).toBe(0);
  });

  it('过滤悬空边、去重无向重复边', () => {
    const out = computeWikiGraphLayout({
      nodes: [
        { id: 'a', title: 'A', type: 'entity' },
        { id: 'b', title: 'B', type: 'entity' },
      ],
      edges: [
        { source: 'a', target: 'b', relation: 'related' },
        { source: 'b', target: 'a', relation: 'mentions' },
        { source: 'a', target: 'ghost', relation: 'related' },
      ],
      width: 1600,
      height: 1100,
    });
    expect(out.edges.length).toBe(1);
    expect(out.edges[0]).toEqual({ source: 'a', target: 'b', relation: 'related' });
  });

  it('空节点数组不抛错，返回空布局', () => {
    const out = computeWikiGraphLayout({ nodes: [], edges: [], width: 1600, height: 1100 });
    expect(out.nodes).toEqual([]);
    expect(out.edges).toEqual([]);
  });

  it('estimateGraphTextWidth 区分 CJK 与 ASCII', () => {
    expect(estimateGraphTextWidth('中')).toBe(15);
    expect(estimateGraphTextWidth('a')).toBe(8);
    expect(estimateGraphTextWidth('中a')).toBe(23);
  });
});

// ── 图谱视图（happy-dom，无 Worker 走同步布局回退） ──

describe('图谱数据加载与渲染', () => {
  it('切到图谱 tab：拉取 graph 并渲染节点 / 边 / 箭头 / 工具栏', async () => {
    await openGraphView();

    expect(api.wikiGraph).toHaveBeenCalledWith('default');
    // 标签默认「显示来源节点」勾选
    const toggle = root().querySelector('[data-graph-toggle-sources]') as HTMLInputElement;
    expect(toggle.checked).toBe(true);

    const nodes = root().querySelectorAll('.wiki-graph-node');
    expect(nodes.length).toBe(3);
    // 节点标签文本（<text> 原生标签）；report.pdf 超出 source 节点宽度被截断，全文进 <title>
    const labels = Array.from(root().querySelectorAll('.wiki-graph-node__label')).map((el) => el.textContent);
    expect(labels).toContain('React 笔记');
    expect(labels).toContain('Vue 笔记');
    expect(labels).toContain('repo…');
    expect(root().querySelector('[data-node-id="s1"] title')?.textContent).toBe('report.pdf');
    // 类型着色类
    expect(root().querySelector('[data-node-id="p1"]')?.classList.contains('wiki-graph-node--entity')).toBe(true);
    expect(root().querySelector('[data-node-id="p2"]')?.classList.contains('wiki-graph-node--entity')).toBe(true);
    expect(root().querySelector('[data-node-id="s1"]')?.classList.contains('wiki-graph-node--source')).toBe(true);

    // 边：2 条；source_of 带箭头
    const edges = root().querySelectorAll('.wiki-graph-edge');
    expect(edges.length).toBe(2);
    const sourceEdge = root().querySelector('.wiki-graph-edge--source_of');
    expect(sourceEdge?.getAttribute('marker-end')).toBe('url(#wiki-graph-arrowhead)');
    expect(root().querySelector('.wiki-graph-edge--related')?.getAttribute('marker-end')).toBeNull();

    // 状态栏统计：3 节点 · 2 关系
    expect(root().querySelector('.wiki-graph__stats')?.textContent).toContain('3 节点');
    expect(root().querySelector('.wiki-graph__stats')?.textContent).toContain('2 关系');
    // 视图 tab 出现「图谱」且为当前选中
    expect(root().querySelector('[data-wiki-view="graph"]')?.classList.contains('is-active')).toBe(true);
  });

  it('图谱加载失败：显示错误与重试按钮，重试后恢复', async () => {
    api.wikiGraph.mockRejectedValueOnce(new Error('服务不可用'));
    await refreshWikiData();
    root().querySelector('[data-wiki-view="graph"]')?.dispatchEvent(new Event('click'));
    await vi.waitFor(
      () => {
        expect(root().querySelector('.wiki-graph__overlay')?.textContent).toContain('加载图谱失败');
        expect(root().querySelector('.wiki-graph__overlay')?.textContent).toContain('服务不可用');
      },
      { timeout: 10000, interval: 20 },
    );

    root().querySelector('[data-graph-retry]')?.dispatchEvent(new Event('click'));
    await vi.waitFor(() => expect(root().querySelector('.wiki-graph__svg')).not.toBeNull(), {
      timeout: 10000,
      interval: 20,
    });
    expect(api.wikiGraph.mock.calls.length).toBe(2);
  });

  it('空图谱：显示空态文案', async () => {
    api.wikiGraph.mockResolvedValue({ ok: true, graph: { nodes: [], edges: [] } });
    await refreshWikiData();
    root().querySelector('[data-wiki-view="graph"]')?.dispatchEvent(new Event('click'));
    await vi.waitFor(
      () => {
        expect(root().querySelector('.wiki-graph__overlay')?.textContent).toContain('当前知识库没有页面');
      },
      { timeout: 10000, interval: 20 },
    );
  });
});

describe('节点交互', () => {
  it('点击页面节点：选中并加载右栏详情，节点高亮', async () => {
    await openGraphView();

    root().querySelector('[data-node-id="p1"]')?.dispatchEvent(new Event('click'));
    await vi.waitFor(
      () => {
        expect(api.wikiPage).toHaveBeenCalledWith('p1', 'default');
        expect((root().querySelector('.wiki-detail__title') as HTMLInputElement | null)?.value).toBe('React 笔记');
      },
      { timeout: 10000, interval: 20 },
    );
    // 选中环 + active 类
    expect(root().querySelector('[data-node-id="p1"]')?.classList.contains('wiki-graph-node--active')).toBe(true);
    expect(root().querySelector('[data-node-id="p1"] .wiki-graph-node__ring')).not.toBeNull();
  });

  it('点击 source 节点不触发选中', async () => {
    await openGraphView();
    api.wikiPage.mockClear();

    root().querySelector('[data-node-id="s1"]')?.dispatchEvent(new Event('click'));
    await flush();
    expect(api.wikiPage).not.toHaveBeenCalled();
    expect(root().querySelector('.wiki-detail__title')).toBeNull();
  });

  it('隐藏来源开关：source 节点与相关边消失，统计显示隐藏数', async () => {
    await openGraphView();

    const toggle = root().querySelector('[data-graph-toggle-sources]') as HTMLInputElement;
    toggle.checked = false;
    toggle.dispatchEvent(new Event('change'));

    await vi.waitFor(
      () => {
        expect(root().querySelectorAll('.wiki-graph-node').length).toBe(2);
        expect(root().querySelector('[data-node-id="s1"]')).toBeNull();
      },
      { timeout: 10000, interval: 20 },
    );
    // source_of 边随 source 节点一起被过滤
    expect(root().querySelectorAll('.wiki-graph-edge').length).toBe(1);
    expect(root().querySelector('.wiki-graph__stats')?.textContent).toContain('1 来源已隐藏');

    // 再打开恢复
    const toggle2 = root().querySelector('[data-graph-toggle-sources]') as HTMLInputElement;
    toggle2.checked = true;
    toggle2.dispatchEvent(new Event('change'));
    await vi.waitFor(() => expect(root().querySelectorAll('.wiki-graph-node').length).toBe(3), {
      timeout: 10000,
      interval: 20,
    });
  });
});

describe('视口缩放与平移', () => {
  const viewport = () => root().querySelector('[data-graph-viewport]') as Element;
  const scaleOf = () => Number(/scale\(([\d.]+)\)/.exec(viewport().getAttribute('transform') ?? '')?.[1]);

  it('滚轮缩放：上滚放大 1.15 倍并更新状态栏百分比', async () => {
    await openGraphView();
    const before = scaleOf();
    expect(before).toBeGreaterThan(0);

    const svg = root().querySelector('[data-graph-svg]') as SVGSVGElement;
    svg.dispatchEvent(new WheelEvent('wheel', { deltaY: -100, clientX: 10, clientY: 10, cancelable: true }));
    expect(scaleOf()).toBeCloseTo(before * 1.15, 5);
    expect(root().querySelector('.wiki-graph__stats')?.textContent).toContain(`${Math.round(before * 1.15 * 100)}%`);

    svg.dispatchEvent(new WheelEvent('wheel', { deltaY: 100, clientX: 10, clientY: 10, cancelable: true }));
    expect(scaleOf()).toBeCloseTo(before * 1.15 * 0.87, 5);
  });

  it('缩放条：+ / − / 1:1 按钮更新 transform', async () => {
    await openGraphView();
    const before = scaleOf();

    root().querySelector('[data-graph-zoom-in]')?.dispatchEvent(new Event('click'));
    expect(scaleOf()).toBeCloseTo(before * 1.2, 5);

    root().querySelector('[data-graph-reset]')?.dispatchEvent(new Event('click'));
    expect(viewport().getAttribute('transform')).toBe('translate(0, 0) scale(1)');
  });

  it('拖拽空白处平移视口', async () => {
    await openGraphView();
    const before = viewport().getAttribute('transform') ?? '';

    const svg = root().querySelector('[data-graph-svg]') as SVGSVGElement;
    svg.dispatchEvent(new MouseEvent('mousedown', { clientX: 100, clientY: 100, bubbles: true, cancelable: true }));
    svg.dispatchEvent(new MouseEvent('mousemove', { clientX: 130, clientY: 120, bubbles: true }));
    svg.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));

    const after = viewport().getAttribute('transform') ?? '';
    expect(after).not.toBe(before);
    // happy-dom 下 getBoundingClientRect 全 0 → scaleBase=1，位移即像素差（+30, +20）
    const match = /translate\((-?[\d.]+), (-?[\d.]+)\)/.exec(after);
    const beforeMatch = /translate\((-?[\d.]+), (-?[\d.]+)\)/.exec(before);
    expect(Number(match?.[1]) - Number(beforeMatch?.[1])).toBeCloseTo(30, 5);
    expect(Number(match?.[2]) - Number(beforeMatch?.[2])).toBeCloseTo(20, 5);

    // 平移结束后再移动不再变化
    svg.dispatchEvent(new MouseEvent('mousemove', { clientX: 400, clientY: 400, bubbles: true }));
    expect(viewport().getAttribute('transform')).toBe(after);
  });
});
