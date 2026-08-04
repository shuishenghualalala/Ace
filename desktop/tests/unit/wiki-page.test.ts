/**
 * @vitest-environment happy-dom
 *
 * wiki-page 单测：三栏浏览与 Agent-first 上传边界（对齐 web WikiHub，无页面编辑/新建入口）。
 * 覆盖：KB 加载渲染、三种视图切换、分页加载更多、点击条目加载详情、错误 notify、
 *       首次加载失败重试（Phase 2 修复）、上传队列、视频确认、进度帧、单条/批量删除。
 * mock 方式与现有页面单测一致（vi.mock backend-client + happy-dom 容器）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi, type WikiPage } from '../../src/ui/backend-client';

// 本文件含 200 条目的整页 innerHTML 重渲染，全量并行（90+ 测试文件争 CPU）时
// 默认 5s testTimeout 会被 CPU 饥饿打爆（等待均为 waitFor 条件轮询，绿色路径不受影响）。
vi.setConfig({ testTimeout: 20000 });
import {
  __resetWikiViewForTest,
  bindWikiTab,
  buildFileTree,
  groupByType,
  groupPagesByDate,
  refreshWikiData,
  setWikiAgentEntryHandler,
  setWikiAgentKbDeletedHandler,
  summaryOf,
} from '../../src/ui/features/wiki-page';
import { __resetAllStoresForTest, sessionStore } from '../../src/ui/stores/stores';

const { mockShowConfirmDialog } = vi.hoisted(() => ({
  mockShowConfirmDialog: vi.fn(async () => true),
}));

vi.mock('../../src/ui/backend-client', () => ({
  backendApi: {
    wikiKBs: vi.fn(),
    wikiInit: vi.fn(),
    wikiCreateKB: vi.fn(),
    wikiDeleteKB: vi.fn(),
    wikiGraph: vi.fn(),
    wikiPages: vi.fn(),
    wikiPage: vi.fn(),
    wikiSearch: vi.fn(),
    wikiUpdatePage: vi.fn(),
    wikiVaultDocument: vi.fn(),
    wikiSummary: vi.fn(),
    wikiUpload: vi.fn(),
    wikiIngest: vi.fn(),
    wikiCancelIngest: vi.fn(),
    wikiDeletePage: vi.fn(),
    wikiDeletePages: vi.fn(),
  },
}));

vi.mock('../../src/ui/ui-feedback', () => ({
  showConfirmDialog: mockShowConfirmDialog,
}));

const api = backendApi as unknown as {
  wikiKBs: ReturnType<typeof vi.fn>;
  wikiInit: ReturnType<typeof vi.fn>;
  wikiCreateKB: ReturnType<typeof vi.fn>;
  wikiDeleteKB: ReturnType<typeof vi.fn>;
  wikiGraph: ReturnType<typeof vi.fn>;
  wikiPages: ReturnType<typeof vi.fn>;
  wikiPage: ReturnType<typeof vi.fn>;
  wikiSearch: ReturnType<typeof vi.fn>;
  wikiUpdatePage: ReturnType<typeof vi.fn>;
  wikiVaultDocument: ReturnType<typeof vi.fn>;
  wikiSummary: ReturnType<typeof vi.fn>;
  wikiUpload: ReturnType<typeof vi.fn>;
  wikiIngest: ReturnType<typeof vi.fn>;
  wikiCancelIngest: ReturnType<typeof vi.fn>;
  wikiDeletePage: ReturnType<typeof vi.fn>;
  wikiDeletePages: ReturnType<typeof vi.fn>;
};

const mockSelectFile = vi.fn();

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

function pagesResult(pages: WikiPage[]) {
  return { ok: true, pages, source_titles: {}, source_files: {} };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  vi.clearAllMocks();
  mockShowConfirmDialog.mockResolvedValue(true);
  __resetAllStoresForTest();
  sessionStore.set({ activeSessionId: 'sid-1' });
  __resetWikiViewForTest();
  setWikiAgentEntryHandler(null);
  setWikiAgentKbDeletedHandler(null);
  (window as unknown as { Crew: unknown }).Crew = { selectFile: mockSelectFile };
  document.body.innerHTML = `
    <button class="nav-item" data-tab="wiki">Wiki</button>
    <section id="wiki-tab" class="tab-pane"><div id="wiki-page-root"></div></section>
  `;
  api.wikiKBs.mockResolvedValue({
    ok: true,
    kbs: [
      { id: 'default', name: '默认知识库', created_at: 0, updated_at: 0 },
      { id: 'work', name: '工作笔记', created_at: 0, updated_at: 0 },
    ],
  });
  api.wikiInit.mockResolvedValue({ ok: true });
  api.wikiCreateKB.mockResolvedValue({ ok: true, kb: { id: 'notes', name: 'notes', created_at: 0, updated_at: 0 } });
  api.wikiDeleteKB.mockResolvedValue({ ok: true });
  api.wikiGraph.mockResolvedValue({ ok: true, graph: { nodes: [], edges: [] } });
  api.wikiPages.mockResolvedValue(pagesResult([]));
  api.wikiPage.mockResolvedValue({ ok: true, page: makePage({}), source_titles: {}, source_files: {} });
  api.wikiSearch.mockResolvedValue(pagesResult([]));
  api.wikiUpdatePage.mockImplementation(async (_id: string, payload: Partial<WikiPage>) => ({
    ok: true,
    page: makePage({ id: 'p1', ...payload }),
    source_titles: {},
    source_files: {},
  }));
  api.wikiVaultDocument.mockImplementation(async (name: 'Home.md' | 'index.md') => ({
    ok: true,
    document: {
      name,
      path: name,
      content: name === 'Home.md' ? '# 知识库概览\n\n暂无页面。' : '# 知识导航',
      updated_at: NOW,
    },
  }));
  api.wikiSummary.mockResolvedValue({ ok: true, summary: '', kb_id: 'default', status: 'empty' });
  api.wikiDeletePage.mockResolvedValue({ ok: true });
  api.wikiDeletePages.mockResolvedValue({ ok: true, deleted: [], failed: [] });
  api.wikiCancelIngest.mockResolvedValue({ ok: true, cancelled: true });
});

describe('KB 加载与渲染', () => {
  it('加载 KB 列表并默认选中 default，渲染页面列表', async () => {
    api.wikiPages.mockResolvedValue(
      pagesResult([
        makePage({ id: 'p1', title: ' React 笔记', summary: 'Hooks 用法', tags: ['前端'] }),
        makePage({ id: 'p2', title: 'Vue 笔记', page_type: 'entity', file_path: 'vue.md' }),
      ]),
    );
    api.wikiSummary.mockResolvedValue({ ok: true, summary: '这是概览', kb_id: 'default', status: 'ready' });

    await refreshWikiData();
    await flush();

    const select = document.querySelector('#wiki-kb-select') as HTMLSelectElement;
    expect(select).not.toBeNull();
    const options = Array.from(select.options).map((o) => o.value);
    expect(options).toEqual(['default', 'work']);
    expect(select.value).toBe('default');

    const text = document.querySelector('#wiki-page-root')?.textContent ?? '';
    expect(text).toContain('React 笔记');
    expect(text).toContain('Hooks 用法');
    expect(text).toContain('Vue 笔记');
    // 类型徽标 + 时间线分组
    expect(text).toContain('关键词');
    expect(text).toContain('今天');
    // 进入 KB 默认打开 Home.md，并以中文名称展示。
    expect(text).toContain('知识库概览');
    expect(text).toContain('暂无页面');
    expect(document.querySelector('.wiki-detail.wiki-home-document')).not.toBeNull();
    expect(document.querySelector('.wiki-home-document .wiki-detail__badges')?.textContent).toContain('概览');
    expect(api.wikiVaultDocument).toHaveBeenCalledWith('Home.md', 'default');
    expect(api.wikiInit).toHaveBeenCalledWith('default');
    expect(api.wikiPages).toHaveBeenCalledWith({ limit: 200, offset: 0, kb_id: 'default', brief: true });
  });

  it('已有 KB 首次选中时执行幂等初始化，补齐 Home.md 与 index.md', async () => {
    await refreshWikiData();
    expect(api.wikiInit).toHaveBeenCalledTimes(1);
    expect(api.wikiInit).toHaveBeenCalledWith('default');

    const select = document.querySelector('#wiki-kb-select') as HTMLSelectElement;
    select.value = 'work';
    select.dispatchEvent(new Event('change'));

    await vi.waitFor(() => {
      expect(api.wikiInit).toHaveBeenCalledWith('work');
      expect(api.wikiPages).toHaveBeenCalledWith({ limit: 200, offset: 0, kb_id: 'work', brief: true });
    });
  });

  it('没有任何 KB 时显示空态', async () => {
    api.wikiKBs.mockResolvedValue({ ok: true, kbs: [] });
    await refreshWikiData();
    const text = document.querySelector('#wiki-page-root')?.textContent ?? '';
    expect(text).toContain('暂无知识库');
    expect(api.wikiPages).not.toHaveBeenCalled();
  });

  it('点击 nav tab 触发首次加载', async () => {
    const onTab = vi.fn();
    bindWikiTab(onTab);
    document.querySelector('[data-tab="wiki"]')?.dispatchEvent(new Event('click'));
    expect(onTab).toHaveBeenCalled();
    await vi.waitFor(() => expect(api.wikiKBs).toHaveBeenCalled(), { timeout: 10000, interval: 20 });
  });
});

describe('Home.md 推荐问题', () => {
  const HOME_WITH_QUESTIONS = [
    '# 知识库概览',
    '',
    '> default · 共 2 个页面 · 1 份素材',
    '',
    '这个知识库聚焦多智能体协作。',
    '',
    '## 推荐问题',
    '',
    '- 如何从零配置一个多智能体团队？',
    '- SubAgent 和 Agent Teams 有什么区别？',
    '',
    '## 知识地图',
    '',
    '暂无关键词或话题页面。',
  ].join('\n');

  it('推荐问题小节渲染为提问按钮，点击后发给 Wiki Agent', async () => {
    api.wikiVaultDocument.mockImplementation(async (name: 'Home.md' | 'index.md') => ({
      ok: true,
      document: {
        name,
        path: name,
        content: name === 'Home.md' ? HOME_WITH_QUESTIONS : '# 知识导航',
        updated_at: NOW,
      },
    }));
    const entry = vi.fn();
    setWikiAgentEntryHandler(entry);

    await refreshWikiData();
    await flush();

    const chips = Array.from(
      document.querySelectorAll<HTMLButtonElement>('.wiki-home-document .wiki-ask-chip'),
    );
    expect(chips.map((c) => c.textContent)).toEqual([
      '如何从零配置一个多智能体团队？',
      'SubAgent 和 Agent Teams 有什么区别？',
    ]);
    // 原「推荐问题」h2 + ul 已被替换为按钮组
    const headings = Array.from(document.querySelectorAll('.wiki-home-document h2'))
      .map((h) => h.textContent?.trim());
    expect(headings).not.toContain('推荐问题');
    expect(headings).toContain('知识地图');

    chips[0].click();
    expect(entry).toHaveBeenCalledWith({
      kbId: 'default',
      kbName: '默认知识库',
      prompt: '如何从零配置一个多智能体团队？',
    });
  });

  it('没有推荐问题小节时不生成按钮组', async () => {
    await refreshWikiData();
    await flush();
    expect(document.querySelector('.wiki-ask-chips')).toBeNull();
  });
});

describe('三种视图切换', () => {
  beforeEach(async () => {
    api.wikiPages.mockResolvedValue(
      pagesResult([
        makePage({ id: 'p1', title: 'React', file_path: 'wiki/entities/react.md', page_type: 'entity' }),
        makePage({ id: 'p2', title: 'Vue', file_path: 'wiki/entities/vue.md', page_type: 'entity' }),
      ]),
    );
    await refreshWikiData();
  });

  function clickView(key: string): void {
    document.querySelector(`[data-wiki-view="${key}"]`)?.dispatchEvent(new Event('click'));
  }

  it('默认时间线视图，按日期分组', () => {
    const root = document.querySelector('#wiki-page-root');
    expect(root?.querySelector('.wiki-tl-group__header')?.textContent).toBe('今天');
    expect(root?.querySelectorAll('.wiki-item').length).toBe(2);
  });

  it('文件树视图：目录折叠 → 点击展开 → 显示页面', () => {
    clickView('tree');
    const root = document.querySelector('#wiki-page-root');
    // 知识库默认展开，知识分类折叠；目录与根文档均使用中文显示名。
    expect(root?.querySelector('.wiki-tree__folder-name')?.textContent).toBe('知识库');
    expect(Array.from(root?.querySelectorAll('.wiki-tree__title') ?? []).map((node) => node.textContent)).toEqual([
      '知识库概览',
      '知识导航',
    ]);
    expect(root?.querySelectorAll('.wiki-tree__item').length).toBe(2);
    // 展开 entities
    root?.querySelector('[data-tree-path="wiki/entities"]')?.dispatchEvent(new Event('click'));
    const items = document.querySelectorAll('.wiki-tree__item');
    expect(items.length).toBe(4);
    // 再点一次折叠
    document.querySelector('[data-tree-path="wiki/entities"]')?.dispatchEvent(new Event('click'));
    expect(document.querySelectorAll('.wiki-tree__item').length).toBe(2);
  });

  it('文件树视图：文件夹显示笔记数量，空文件夹不显示', () => {
    clickView('tree');
    const root = document.querySelector('#wiki-page-root');
    const entitiesToggle = root?.querySelector('[data-tree-path="wiki/entities"]');
    expect(entitiesToggle?.querySelector('.wiki-tree__folder-count')?.textContent).toBe('2');
    // 递归统计：根「知识库」文件夹同样显示总数
    const rootToggle = root?.querySelector('[data-tree-path="wiki"]');
    expect(rootToggle?.querySelector('.wiki-tree__folder-count')?.textContent).toBe('2');
    // 无笔记的文件夹不渲染数量
    const topicsToggle = root?.querySelector('[data-tree-path="wiki/topics"]');
    expect(topicsToggle?.querySelector('.wiki-tree__folder-count')).toBeNull();
  });

  it('点击卡片后列表滚动位置保持，切换视图后归零', async () => {
    const scrollEl = () => document.querySelector<HTMLElement>('.wiki-list-scroll');
    const list = scrollEl();
    expect(list).not.toBeNull();
    if (list) list.scrollTop = 321;

    document.querySelector('[data-page-id="p1"]')?.dispatchEvent(new Event('click'));
    await flush();
    await flush();
    expect(scrollEl()?.scrollTop).toBe(321);

    clickView('tree');
    expect(scrollEl()?.scrollTop).toBe(0);
  });

  it('文件树中的知识库概览可以打开并渲染 Home.md 正文', async () => {
    clickView('tree');
    document.querySelector('[data-vault-document="Home.md"]')?.dispatchEvent(new Event('click'));

    await vi.waitFor(() => {
      expect(api.wikiVaultDocument).toHaveBeenCalledWith('Home.md', 'default');
      expect(document.querySelector('.wiki-detail__title')?.textContent).toBe('知识库概览');
      expect(document.querySelector('.wiki-detail__content')?.textContent).toContain('暂无页面');
    });
  });

  it('类型视图：按 page_type 分组并显示计数', () => {
    clickView('type');
    const root = document.querySelector('#wiki-page-root');
    const titles = Array.from(root?.querySelectorAll('.wiki-type-section__title') ?? []).map(
      (el) => el.textContent,
    );
    expect(titles.some((t) => t?.includes('关键词'))).toBe(true);
    expect(titles).toHaveLength(1);
    expect(titles[0]).toContain('2');
  });

  it('切回时间线视图', () => {
    clickView('tree');
    clickView('timeline');
    expect(document.querySelector('.wiki-tl-group__header')?.textContent).toBe('今天');
  });
});

describe('分页加载更多', () => {
  it('首屏满 PAGE_LIMIT 显示「加载更多」，点击后追加并消失', async () => {
    const firstBatch = Array.from({ length: 200 }, (_, i) =>
      makePage({ id: `p${i}`, title: `页面 ${i}`, file_path: `f/${i}.md` }),
    );
    api.wikiPages
      .mockResolvedValueOnce(pagesResult(firstBatch))
      .mockResolvedValueOnce(pagesResult([makePage({ id: 'p200', title: '最后一页', file_path: 'f/200.md' })]));

    await refreshWikiData();
    const loadMoreBtn = document.querySelector('[data-load-more]') as HTMLButtonElement | null;
    expect(loadMoreBtn).not.toBeNull();

    loadMoreBtn?.dispatchEvent(new Event('click'));

    // 全量并行高负载下固定 flush 偶发超时，用 waitFor 等异步渲染落定。
    await vi.waitFor(
      () => {
        expect(api.wikiPages).toHaveBeenLastCalledWith({ limit: 200, offset: 200, kb_id: 'default', brief: true });
        const text = document.querySelector('#wiki-page-root')?.textContent ?? '';
        expect(text).toContain('最后一页');
        // 第二页不足 PAGE_LIMIT → hasMore=false → 按钮消失
        expect(document.querySelector('[data-load-more]')).toBeNull();
      },
      { timeout: 10000, interval: 20 },
    );
  });
});

describe('点击条目加载详情', () => {
  it('点击列表条目后拉取详情并渲染 Markdown 正文', async () => {
    api.wikiPages.mockResolvedValue(
      pagesResult([makePage({ id: 'p1', title: 'React 笔记', summary: 'brief 摘要' })]),
    );
    api.wikiPage.mockResolvedValue({
      ok: true,
      page: makePage({ id: 'p1', title: 'React 笔记', content: '**粗体正文**', tags: ['前端'] }),
      source_titles: {},
      source_files: {},
    });

    await refreshWikiData();
    document.querySelector('[data-page-id="p1"]')?.dispatchEvent(new Event('click'));

    await vi.waitFor(
      () => {
        expect(api.wikiPage).toHaveBeenCalledWith('p1', 'default');
        const root = document.querySelector('#wiki-page-root');
        expect((root?.querySelector('.wiki-detail__title') as HTMLInputElement | null)?.value).toBe('React 笔记');
        expect(root?.querySelector('.wiki-editor')?.innerHTML).toContain('<strong>粗体正文</strong>');
      },
      { timeout: 10000, interval: 20 },
    );
    const root = document.querySelector('#wiki-page-root');
    // 选中态
    expect(root?.querySelector('.wiki-item.is-active')).not.toBeNull();
  });

  it('正文与标题直接编辑并自动保存 Markdown', async () => {
    vi.useFakeTimers();
    api.wikiPages.mockResolvedValue(pagesResult([makePage({ id: 'p1', title: 'React 笔记' })]));
    api.wikiPage.mockResolvedValue({
      ok: true,
      page: makePage({ id: 'p1', title: 'React 笔记', content: '初始正文', tags: ['前端'] }),
      source_titles: {},
      source_files: {},
    });

    await refreshWikiData();
    document.querySelector('[data-page-id="p1"]')?.dispatchEvent(new Event('click'));
    await vi.runAllTimersAsync();

    const title = document.querySelector<HTMLInputElement>('[data-wiki-title]');
    expect(title?.value).toBe('React 笔记');
    expect(document.querySelector('[contenteditable="true"]')?.textContent).toContain('初始正文');
    if (title) {
      title.value = 'React Hooks';
      title.dispatchEvent(new Event('input'));
    }
    await vi.advanceTimersByTimeAsync(701);

    expect(api.wikiUpdatePage).toHaveBeenCalledWith(
      'p1',
      expect.objectContaining({ title: 'React Hooks', content: '初始正文', tags: ['前端'], sources: [] }),
      'default',
    );
    vi.useRealTimers();
  });

  it('来源显示为只读胶囊并按 Source Page ID 直接跳转', async () => {
    api.wikiPages.mockResolvedValue(pagesResult([makePage({ id: 'p1', title: '入口页' })]));
    api.wikiPage.mockImplementation(async (id: string) => ({
      ok: true,
      page: makePage({ id, title: id === 'p1' ? '入口页' : '目标页', content: '正文' }),
      source_titles: {},
      source_files: {},
      source_pages: id === 'p1'
        ? [{ id: 'src_p2', title: '意大利-法国旅行行程单', page_type: 'source' }]
        : [],
      relation_pages: [],
    }));

    await refreshWikiData();
    document.querySelector('[data-page-id="p1"]')?.dispatchEvent(new Event('click'));
    await vi.waitFor(() => {
      expect(document.querySelector('[data-source-page-id="src_p2"]')?.textContent)
        .toContain('意大利-法国旅行行程单');
    });
    expect(document.querySelector('[data-wiki-tags]')).toBeNull();
    expect(document.querySelector('[data-wiki-sources]')).toBeNull();

    document.querySelector('[data-source-page-id="src_p2"]')
      ?.dispatchEvent(new MouseEvent('click', { bubbles: true }));

    await vi.waitFor(() => {
      expect(api.wikiPage).toHaveBeenCalledWith('src_p2', 'default');
      expect((document.querySelector('[data-wiki-title]') as HTMLInputElement | null)?.value).toBe('目标页');
    });
    expect(api.wikiSearch).not.toHaveBeenCalled();
  });

  it('展示知识节点关系并过滤已由来源区承载的摘要页', async () => {
    api.wikiPages.mockResolvedValue(pagesResult([makePage({ id: 'p1', title: '罗马' })]));
    api.wikiPage.mockImplementation(async (id: string) => ({
      ok: true,
      page: makePage({ id, title: id === 'p1' ? '罗马' : '意大利城市旅行', content: '正文' }),
      source_titles: {},
      source_files: {},
      source_pages: [],
      relation_pages: id === 'p1'
        ? [
            { id: 'topic_1', title: '意大利城市旅行', page_type: 'topic', relation: 'part_of', direction: 'outgoing' },
            { id: 'source_1', title: '意大利旅行行程单', page_type: 'source', relation: 'describes', direction: 'incoming' },
          ]
        : [],
    }));

    await refreshWikiData();
    document.querySelector('[data-page-id="p1"]')?.dispatchEvent(new Event('click'));
    await vi.waitFor(() => {
      const related = document.querySelector('.wiki-related-pages')?.textContent ?? '';
      expect(related).toContain('意大利城市旅行');
      expect(related).toContain('话题 · 属于');
      expect(related).not.toContain('意大利旅行行程单');
    });

    document.querySelector('[data-related-page-id="topic_1"]')
      ?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await vi.waitFor(() => {
      expect(api.wikiPage).toHaveBeenCalledWith('topic_1', 'default');
    });
  });

  it('只浏览不编辑时不触发自动保存（不刷 updated_at）', async () => {
    api.wikiPages.mockResolvedValue(
      pagesResult([makePage({ id: 'p1', title: '页面一' }), makePage({ id: 'p2', title: '页面二' })]),
    );
    api.wikiPage.mockImplementation(async (id: string) => ({
      ok: true,
      page: makePage({ id, title: id === 'p1' ? '页面一' : '页面二', content: '正文' }),
      source_titles: {},
      source_files: {},
    }));

    await refreshWikiData();
    document.querySelector('[data-page-id="p1"]')?.dispatchEvent(new Event('click'));
    await vi.waitFor(() => {
      expect(document.querySelector('[contenteditable="true"]')).not.toBeNull();
    });
    // 未编辑直接切到另一页：不应有任何保存请求
    document.querySelector('[data-page-id="p2"]')?.dispatchEvent(new Event('click'));
    await flush();
    expect(api.wikiUpdatePage).not.toHaveBeenCalled();
  });

  it('正文 WikiLink 不在首批列表时通过搜索解析并打开', async () => {
    api.wikiPages.mockResolvedValue(pagesResult([makePage({ id: 'p1', title: '入口页' })]));
    api.wikiPage.mockResolvedValue({
      ok: true,
      page: makePage({ id: 'p1', title: '入口页', content: '参见 [[目标页]]' }),
      source_titles: {},
      source_files: {},
    });
    api.wikiSearch.mockResolvedValue(
      pagesResult([makePage({ id: 'p999', title: '目标页', aliases: ['目标别名'], content: '目标正文' })]),
    );

    await refreshWikiData();
    document.querySelector('[data-page-id="p1"]')?.dispatchEvent(new Event('click'));
    await vi.waitFor(() => expect(document.querySelector('a[href="wiki:%E7%9B%AE%E6%A0%87%E9%A1%B5"]')).not.toBeNull());
    document.querySelector('a[href="wiki:%E7%9B%AE%E6%A0%87%E9%A1%B5"]')
      ?.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));

    await vi.waitFor(() => {
      expect(api.wikiSearch).toHaveBeenCalledWith('目标页', 'default', 8);
      expect((document.querySelector('[data-wiki-title]') as HTMLInputElement | null)?.value).toBe('目标页');
    });
  });

  it('详情加载失败时 notify 提示', async () => {
    api.wikiPages.mockResolvedValue(pagesResult([makePage({ id: 'p1', title: 'React 笔记' })]));
    api.wikiPage.mockRejectedValue(new Error('404 页面不存在'));

    await refreshWikiData();
    document.querySelector('[data-page-id="p1"]')?.dispatchEvent(new Event('click'));

    await vi.waitFor(
      () => {
        const toasts = Array.from(document.querySelectorAll('.ui-toast'));
        expect(toasts.some((t) => t.textContent?.includes('加载页面详情失败'))).toBe(true);
      },
      { timeout: 10000, interval: 20 },
    );
  });
});

describe('错误处理', () => {
  it('KB 加载失败时 notify 提示并显示「加载失败」空态（而非误导的「暂无知识库」）', async () => {
    api.wikiKBs.mockRejectedValue(new Error('网络异常'));
    await refreshWikiData();

    await vi.waitFor(
      () => {
        const toasts = Array.from(document.querySelectorAll('.ui-toast'));
        expect(toasts.some((t) => t.textContent?.includes('加载知识库失败'))).toBe(true);
        expect(toasts.some((t) => t.textContent?.includes('网络异常'))).toBe(true);
        const text = document.querySelector('#wiki-page-root')?.textContent ?? '';
        expect(text).toContain('知识库加载失败');
        expect(text).not.toContain('后端尚未创建任何知识库');
      },
      { timeout: 10000, interval: 20 },
    );
  });

  it('首次加载失败不置 loaded，再次切入 tab 自动重试；成功后不再重复请求', async () => {
    api.wikiKBs.mockRejectedValueOnce(new Error('网络异常'));
    await refreshWikiData();
    expect(api.wikiKBs).toHaveBeenCalledTimes(1);

    // 失败后再次切入 tab：应重试并成功渲染
    await refreshWikiData();
    expect(api.wikiKBs).toHaveBeenCalledTimes(2);
    const text = document.querySelector('#wiki-page-root')?.textContent ?? '';
    expect(text).toContain('知识库还没有内容');

    // 成功后 loaded 置位：再次切入 tab 只重渲染，不再打请求
    await refreshWikiData();
    expect(api.wikiKBs).toHaveBeenCalledTimes(2);
  });
});

describe('边界态（未登录 / 无 KB 自动初始化）', () => {
  it('没有任何 KB 时自动初始化 default 并选中加载（对齐 web WikiHub）', async () => {
    api.wikiKBs
      .mockResolvedValueOnce({ ok: true, kbs: [] })
      .mockResolvedValueOnce({
        ok: true,
        kbs: [{ id: 'default', name: '默认知识库', created_at: 0, updated_at: 0 }],
      });

    await refreshWikiData();

    expect(api.wikiInit).toHaveBeenCalledWith('default');
    expect(api.wikiKBs).toHaveBeenCalledTimes(2);
    const select = document.querySelector('#wiki-kb-select') as HTMLSelectElement;
    expect(select.value).toBe('default');
    expect(api.wikiPages).toHaveBeenCalledWith({ limit: 200, offset: 0, kb_id: 'default', brief: true });
  });

  it('自动初始化失败：失败空态 + notify，且只自动尝试一次', async () => {
    api.wikiKBs.mockResolvedValue({ ok: true, kbs: [] });
    api.wikiInit.mockRejectedValue(new Error('init 不可用'));

    await refreshWikiData();
    // notify 的 16ms 节流计时器在高负载下会跨用例迟到（僵尸 toast），
    // 不能取第一个 toast 断言，必须匹配「存在目标文案的 toast」。
    await vi.waitFor(
      () => {
        const toasts = Array.from(document.querySelectorAll('.ui-toast'));
        expect(toasts.some((t) => t.textContent?.includes('初始化默认知识库失败'))).toBe(true);
        const text = document.querySelector('#wiki-page-root')?.textContent ?? '';
        expect(text).toContain('知识库加载失败');
      },
      { timeout: 10000, interval: 20 },
    );

    // 失败后 loaded 未置位，再次切入 tab 会重新拉 KB 列表，但不再自动 init
    await refreshWikiData();
    expect(api.wikiKBs).toHaveBeenCalledTimes(2);
    expect(api.wikiInit).toHaveBeenCalledTimes(1);
  });
});

describe('新建知识库', () => {
  function openCreateForm(): HTMLInputElement {
    document.querySelector('[data-kb-create-toggle]')?.dispatchEvent(new Event('click'));
    return document.querySelector('[data-kb-create-input]') as HTMLInputElement;
  }

  function typeInto(input: HTMLInputElement, value: string): void {
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  it('非法 ID（含路径分隔符）提示且不发起请求', async () => {
    await refreshWikiData();
    const input = openCreateForm();
    expect(input).not.toBeNull();
    typeInto(input, 'bad/id');
    document.querySelector('[data-kb-create-submit]')?.dispatchEvent(new Event('click'));
    await vi.waitFor(
      () => {
        const toasts = Array.from(document.querySelectorAll('.ui-toast'));
        expect(toasts.some((t) => t.textContent?.includes('不能包含'))).toBe(true);
      },
      { timeout: 10000, interval: 20 },
    );
    expect(api.wikiCreateKB).not.toHaveBeenCalled();
  });

  it('中文 ID 合法（对齐后端 normalize_kb_id：不限字符集，kb_id 即目录名）', async () => {
    api.wikiKBs.mockResolvedValue({
      ok: true,
      kbs: [
        { id: 'default', name: '默认知识库', created_at: 0, updated_at: 0 },
        { id: '工作笔记', name: '工作笔记', created_at: 0, updated_at: 0 },
      ],
    });
    api.wikiCreateKB.mockResolvedValue({ ok: true, kb: { id: '工作笔记', name: '工作笔记', created_at: 0, updated_at: 0 } });
    await refreshWikiData();
    const input = openCreateForm();
    typeInto(input, '工作笔记');
    document.querySelector('[data-kb-create-submit]')?.dispatchEvent(new Event('click'));

    await vi.waitFor(
      () => {
        expect(api.wikiCreateKB).toHaveBeenCalledWith({ kb_id: '工作笔记', name: '工作笔记' });
        const select = document.querySelector('#wiki-kb-select') as HTMLSelectElement;
        expect(select.value).toBe('工作笔记');
      },
      { timeout: 10000, interval: 20 },
    );
  });

  it('创建成功后刷新列表并选中新 KB', async () => {
    api.wikiKBs.mockResolvedValue({
      ok: true,
      kbs: [
        { id: 'default', name: '默认知识库', created_at: 0, updated_at: 0 },
        { id: 'notes', name: 'notes', created_at: 0, updated_at: 0 },
      ],
    });
    await refreshWikiData();
    const input = openCreateForm();
    typeInto(input, 'notes');
    document.querySelector('[data-kb-create-submit]')?.dispatchEvent(new Event('click'));

    await vi.waitFor(
      () => {
        expect(api.wikiCreateKB).toHaveBeenCalledWith({ kb_id: 'notes', name: 'notes' });
        const select = document.querySelector('#wiki-kb-select') as HTMLSelectElement;
        expect(select.value).toBe('notes');
        // 成功后表单关闭
        expect(document.querySelector('[data-kb-create-input]')).toBeNull();
      },
      { timeout: 10000, interval: 20 },
    );
    expect(api.wikiPages).toHaveBeenCalledWith({ limit: 200, offset: 0, kb_id: 'notes', brief: true });
  });

  it('后端拒绝时 notify 错误且表单保持打开', async () => {
    api.wikiCreateKB.mockRejectedValue(new Error('400 知识库已存在'));
    await refreshWikiData();
    const input = openCreateForm();
    typeInto(input, 'default');
    document.querySelector('[data-kb-create-submit]')?.dispatchEvent(new Event('click'));

    await vi.waitFor(
      () => {
        const toasts = Array.from(document.querySelectorAll('.ui-toast'));
        expect(toasts.some((t) => t.textContent?.includes('新建知识库失败'))).toBe(true);
      },
      { timeout: 10000, interval: 20 },
    );
    expect(document.querySelector('[data-kb-create-input]')).not.toBeNull();
  });

  it('取消 / Escape 关闭表单并清空草稿', async () => {
    await refreshWikiData();
    const input = openCreateForm();
    typeInto(input, 'abc');
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(document.querySelector('[data-kb-create-input]')).toBeNull();
    // 重新打开：草稿已清空
    const input2 = openCreateForm();
    expect(input2.value).toBe('');
    document.querySelector('[data-kb-create-cancel]')?.dispatchEvent(new Event('click'));
    expect(document.querySelector('[data-kb-create-input]')).toBeNull();
  });
});

describe('删除知识库', () => {
  const kbs2 = {
    ok: true,
    kbs: [
      { id: 'default', name: '默认知识库', created_at: 0, updated_at: 0 },
      { id: 'work', name: '工作笔记', created_at: 0, updated_at: 0 },
      { id: 'tutorial', name: 'LLM Wiki 使用教程', created_at: 0, updated_at: 0 },
    ],
  };
  const kbs1 = { ok: true, kbs: [{ id: 'default', name: '默认知识库', created_at: 0, updated_at: 0 }] };

  function selectKb(id: string): void {
    const select = document.querySelector('#wiki-kb-select') as HTMLSelectElement;
    select.value = id;
    select.dispatchEvent(new Event('change'));
  }

  it('选中 default 时删除按钮禁用（后端禁止删 default）', async () => {
    await refreshWikiData();
    const btn = document.querySelector('[data-kb-delete]') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it('确认后删除当前 KB 并回落选中 default', async () => {
    const onDeleted = vi.fn();
    setWikiAgentKbDeletedHandler(onDeleted);
    api.wikiKBs
      .mockResolvedValueOnce(kbs2) // 首次加载
      .mockResolvedValueOnce(kbs2) // 切到 work 后的 reloadAll
      .mockResolvedValue(kbs1); // 删除后的 reloadAll
    await refreshWikiData();
    selectKb('work');
    await vi.waitFor(
      () => expect((document.querySelector('#wiki-kb-select') as HTMLSelectElement).value).toBe('work'),
      { timeout: 10000, interval: 20 },
    );

    document.querySelector('[data-kb-delete]')?.dispatchEvent(new Event('click'));
    await vi.waitFor(
      () => {
        expect(mockShowConfirmDialog).toHaveBeenCalled();
        expect(mockShowConfirmDialog.mock.calls[0][0].message).toContain('专属 Wiki 问答历史');
        expect(api.wikiDeleteKB).toHaveBeenCalledWith('work');
        expect(onDeleted).toHaveBeenCalledWith('work');
        const select = document.querySelector('#wiki-kb-select') as HTMLSelectElement;
        expect(select.value).toBe('default');
      },
      { timeout: 10000, interval: 20 },
    );
  });

  it('取消确认则不删除', async () => {
    mockShowConfirmDialog.mockResolvedValue(false);
    await refreshWikiData();
    selectKb('work');
    await vi.waitFor(
      () => expect((document.querySelector('#wiki-kb-select') as HTMLSelectElement).value).toBe('work'),
      { timeout: 10000, interval: 20 },
    );
    document.querySelector('[data-kb-delete]')?.dispatchEvent(new Event('click'));
    await flush();
    expect(api.wikiDeleteKB).not.toHaveBeenCalled();
  });
});

describe('分栏拖拽', () => {
  it('列表模式与图谱模式都有把手', async () => {
    await refreshWikiData();
    expect(document.querySelector('[data-wiki-browser-sash]')).not.toBeNull();
    document.querySelector('[data-wiki-view="graph"]')?.dispatchEvent(new Event('click'));
    expect(document.querySelector('[data-wiki-browser-sash]')).not.toBeNull();
  });

  it('图谱模式：默认沿用面板宽度，拖拽后固定像素并持久化，双击复位回默认', async () => {
    await refreshWikiData();
    document.querySelector('[data-wiki-view="graph"]')?.dispatchEvent(new Event('click'));
    const pane = document.querySelector('.wiki-browser-pane') as HTMLElement;
    // 未拖拽：沿用知识库面板默认宽度
    expect(pane.style.width).toBe('620px');

    const sash = document.querySelector('[data-wiki-browser-sash]') as HTMLElement;
    // happy-dom 的 getBoundingClientRect 恒为 0：startX=500 → move 到 0，sign=-1 即拖出 500px
    sash.dispatchEvent(new MouseEvent('mousedown', { clientX: 500, bubbles: true }));
    document.dispatchEvent(new MouseEvent('mousemove', { clientX: 0, bubbles: true }));
    expect(pane.style.width).toBe('500px');
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    expect(localStorage.getItem('crew.desktop.wikiGraphWidth.v1')).toBe('500');

    // 重渲染后固定宽度存活
    document.querySelector('[data-wiki-view="timeline"]')?.dispatchEvent(new Event('click'));
    document.querySelector('[data-wiki-view="graph"]')?.dispatchEvent(new Event('click'));
    const rebuilt = document.querySelector('.wiki-browser-pane') as HTMLElement;
    expect(rebuilt.style.width).toBe('500px');

    // 双击复位回默认面板宽度
    document.querySelector('[data-wiki-browser-sash]')?.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    const reset = document.querySelector('.wiki-browser-pane') as HTMLElement;
    expect(reset.style.width).toBe('620px');
    expect(localStorage.getItem('crew.desktop.wikiGraphWidth.v1')).toBeNull();
  });

  it('拖拽调宽并持久化，超过下限被钳制', async () => {
    await refreshWikiData();
    const pane = document.querySelector('.wiki-browser-pane') as HTMLElement;
    expect(pane.style.width).toBe('620px');

    const sash = document.querySelector('[data-wiki-browser-sash]') as HTMLElement;
    // 把手在面板左缘（sign=-1）：向右拖变窄
    sash.dispatchEvent(new MouseEvent('mousedown', { clientX: 500, bubbles: true }));
    document.dispatchEvent(new MouseEvent('mousemove', { clientX: 560, bubbles: true }));
    expect(pane.style.width).toBe('560px');
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    expect(localStorage.getItem('crew.desktop.wikiBrowserWidth.v1')).toBe('560');

    // 大幅右拖超过下限 → 钳到 420
    sash.dispatchEvent(new MouseEvent('mousedown', { clientX: 500, bubbles: true }));
    document.dispatchEvent(new MouseEvent('mousemove', { clientX: 5000, bubbles: true }));
    expect(pane.style.width).toBe('420px');
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    expect(localStorage.getItem('crew.desktop.wikiBrowserWidth.v1')).toBe('420');
  });

  it('双击复位默认宽度并持久化', async () => {
    await refreshWikiData();
    const sash = document.querySelector('[data-wiki-browser-sash]') as HTMLElement;
    sash.dispatchEvent(new MouseEvent('mousedown', { clientX: 500, bubbles: true }));
    document.dispatchEvent(new MouseEvent('mousemove', { clientX: 600, bubbles: true }));
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));

    sash.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    const pane = document.querySelector('.wiki-browser-pane') as HTMLElement;
    expect(pane.style.width).toBe('620px');
    expect(localStorage.getItem('crew.desktop.wikiBrowserWidth.v1')).toBe('620');
  });
});

describe('知识库面板收起/展开', () => {
  it('页头按钮收起后面板与把手被隐藏（DOM 保留），再点展开恢复', async () => {
    await refreshWikiData();
    expect(document.querySelector('.page-shell--wiki')?.classList.contains('wiki-browser-collapsed')).toBe(false);

    document.querySelector<HTMLElement>('[data-wiki-browser-toggle]')?.click();
    const shell = document.querySelector('.page-shell--wiki') as HTMLElement;
    expect(shell.classList.contains('wiki-browser-collapsed')).toBe(true);
    // 收起走 CSS 隐藏：面板 DOM 仍在，详情保活逻辑不受影响
    expect(document.querySelector('.wiki-browser-pane')).not.toBeNull();

    document.querySelector<HTMLElement>('[data-wiki-browser-toggle]')?.click();
    expect(document.querySelector('.page-shell--wiki')?.classList.contains('wiki-browser-collapsed')).toBe(false);
  });
});

describe('Wiki Agent 对话面板（左对话 / 右知识库面板布局）', () => {
  it('对话主区常驻，页头无「问 Wiki」按钮', async () => {
    await refreshWikiData();
    // cmcc 路线：左主区是 wiki-agent.ts 挂载的 .wiki-agent-pane，常驻不拖拽。
    const pane = document.querySelector('.wiki-agent-pane') as HTMLElement;
    expect(pane).not.toBeNull();
    expect(pane.getAttribute('data-wiki-agent-panel')).not.toBeNull();
    expect(document.querySelector('[data-wiki-agent]')).toBeNull();
  });

  it('图谱模式：对话面板与知识库面板把手共存', async () => {
    await refreshWikiData();
    document.querySelector('[data-wiki-view="graph"]')?.dispatchEvent(new Event('click'));
    expect(document.querySelector('[data-wiki-browser-sash]')).not.toBeNull();
    expect(document.querySelector('.wiki-agent-pane')).not.toBeNull();
  });
});

describe('移植的纯逻辑（wikiTree）', () => {
  it('buildFileTree 只展示 Wiki Vault，并预置来源类型与根文档', () => {
    const root = buildFileTree([
      makePage({ id: 'p1', title: '论文', file_path: 'wiki/sources/pdfs/论文.md', page_type: 'source' }),
      makePage({ id: 'p2', title: '实体', file_path: 'wiki/entities/实体.md', page_type: 'entity' }),
      makePage({ id: 'p3', title: '内部状态', file_path: '.crew/cache/hidden.md' }),
      makePage({ id: 'p4', title: '原始文件', file_path: 'raw/pdfs/raw.md' }),
    ]);

    expect(root.children.map((node) => node.kind === 'folder' ? node.name : node.name)).toEqual([
      'wiki',
      'Home.md',
      'index.md',
    ]);
    const wiki = root.children[0];
    expect(wiki.kind).toBe('folder');
    if (wiki.kind === 'folder') {
      expect(wiki.children.filter((node) => node.kind === 'folder').map((node) => node.name)).toEqual([
        'entities',
        'topics',
        'sources',
        'comparisons',
        'synthesis',
      ]);
      const sources = wiki.children.find(
        (node) => node.kind === 'folder' && node.name === 'sources',
      );
      expect(sources?.kind).toBe('folder');
      if (sources?.kind === 'folder') {
        expect(sources.children.filter((node) => node.kind === 'folder').map((node) => node.name)).toEqual([
          'articles',
          'pdfs',
          'words',
          'excels',
          'ppts',
          'notes',
          'sessions',
          'images',
          'videos',
          'assets',
        ]);
      }
    }
  });

  it('groupByType 按固定顺序分组并按更新时间倒序', () => {
    const groups = groupByType([
      makePage({ id: 'p1', page_type: 'topic', updated_at: 10 }),
      makePage({ id: 'p2', page_type: 'entity', updated_at: 20 }),
      makePage({ id: 'p3', page_type: 'entity', updated_at: 30 }),
    ]);
    expect(groups.map((g) => g.type)).toEqual(['entity', 'topic']);
    expect(groups[0].pages.map((p) => p.id)).toEqual(['p3', 'p2']);
  });

  it('groupPagesByDate 今天/更早分桶', () => {
    const groups = groupPagesByDate([
      makePage({ id: 'p1', updated_at: NOW }),
      makePage({ id: 'p2', updated_at: NOW - 400 * 86400 }),
    ]);
    expect(groups.map((g) => g.label)).toEqual(['今天', '更早']);
  });

  it('summaryOf 优先 summary，空内容回退提示', () => {
    expect(summaryOf(makePage({ summary: '  多  空格  ' }))).toBe('多 空格');
    expect(summaryOf(makePage({}))).toBe('（无内容摘要）');
    expect(summaryOf(makePage({ content: 'x'.repeat(200) })).length).toBe(140);
  });
});

// ── Agent-first 写入边界 ──

describe('Agent-first 写入边界', () => {
  const root = () => document.querySelector('#wiki-page-root') as HTMLElement;

  it('上传只进入 Wiki Composer 附件区，不自动 upload/ingest', async () => {
    const entry = vi.fn();
    setWikiAgentEntryHandler(entry);
    await refreshWikiData();

    root().querySelector('[data-upload]')?.dispatchEvent(new Event('click'));

    expect(entry).toHaveBeenCalledWith({
      kbId: 'default',
      kbName: '默认知识库',
      openAttachment: true,
    });
    expect(api.wikiUpload).not.toHaveBeenCalled();
    expect(api.wikiIngest).not.toHaveBeenCalled();
    expect(root().querySelector('.wiki-upload-jobs')).toBeNull();
  });

  it('渲染单条删除与批量管理入口（default 不渲染编译入口）', async () => {
    api.wikiPages.mockResolvedValue(pagesResult([makePage({ id: 'p1' })]));
    await refreshWikiData();
    // 单条删除按钮（非批量模式下显示）+ 批量管理按钮均存在；直接编译入口不渲染。
    expect(root().querySelector('[data-delete-id]')).not.toBeNull();
    expect(root().querySelector('[data-batch-toggle]')).not.toBeNull();
    expect(root().querySelector('[data-bulk-delete]')).toBeNull();
    expect(root().querySelector('[data-compile]')).toBeNull();
  });

  it('页头不再渲染页面新建/刷新入口，详情不再渲染编辑按钮与文件路径', async () => {
    const page = makePage({ id: 'p1', content: '# 正文' });
    api.wikiPages.mockResolvedValue(pagesResult([page]));
    api.wikiPage.mockResolvedValue({ ok: true, page, source_titles: {}, source_files: {} });
    await refreshWikiData();

    expect(root().querySelector('[data-page-create]')).toBeNull();
    expect(root().querySelector('[data-refresh]')).toBeNull();

    root().querySelector('[data-page-id="p1"]')?.dispatchEvent(new Event('click'));
    await vi.waitFor(() => expect(root().querySelector('.wiki-detail__title')).not.toBeNull());
    expect(root().querySelector('[data-page-edit]')).toBeNull();
    expect(root().querySelector('[data-page-editor]')).toBeNull();
    expect(root().querySelector('.wiki-detail__path')).toBeNull();
  });
});
