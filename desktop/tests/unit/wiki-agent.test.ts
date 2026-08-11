/**
 * @vitest-environment happy-dom
 *
 * wiki-agent 专用 Wiki Agent 对话单测。
 * 覆盖：发送 payload 携带 wiki 参数、wiki_cards 渲染、
 *       页面详情 overlay、建议追问 chips、上传失败反向 prompt、wiki-page 入口挂点、
 *       独立会话管理与登录态边界。
 * mock 方式与 chat-controller-send / wiki-page 单测一致（vi.mock backend-client + happy-dom 容器）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';

// 全量并行（90+ 测试文件争 CPU）时默认 5s testTimeout 会被 CPU 饥饿打爆（与 wiki-page 单测同经验）。
vi.setConfig({ testTimeout: 20000 });

import {
  backendApi,
  type ChatChunk,
  type WikiPage,
} from '../../src/ui/backend-client';
import {
  __resetWikiAgentForTest,
  buildWikiAssistPrompt,
  openWikiAgent,
  forgetWikiAgentKb,
  initWikiAgent,
} from '../../src/ui/features/wiki-agent';
import {
  applyChunk,
  appendMessage,
  setChatCallbacks,
} from '../../src/ui/features/chat-controller';
import { openInspectorToTab } from '../../src/ui/features/inspector';
import { bindFileDrop, bindFilePaste } from '../../src/ui/features/attachments';
import {
  __resetWikiViewForTest,
  refreshWikiData,
  renderWikiPage,
  setWikiAgentEntryHandler,
} from '../../src/ui/features/wiki-page';
import {
  normalizeChunk,
  normalizeWikiCardPages,
  reduceChunk,
  type AnyChatChunk,
  type ReducerSnapshot,
} from '../../src/ui/reducers/chat-reducer';
import { __resetAllStoresForTest, authStore, configStore, messageStore, sessionStore, uiStore, workspaceStore } from '../../src/ui/stores/stores';
import { patchBook, setBookTodos, setBusy, type Bookkeeping } from '../../src/ui/state';

const { mockOpenSession, mockLoadBackendHistory, mockShowConfirmDialog, mockOpenModelSelectPopover } = vi.hoisted(() => ({
  mockOpenSession: vi.fn(),
  mockLoadBackendHistory: vi.fn(async () => undefined),
  mockShowConfirmDialog: vi.fn(async () => true),
  // 模型浮层只验证调用与回调，真实浮层行为在 model-picker-sync.test.ts 覆盖。
  mockOpenModelSelectPopover: vi.fn(() => vi.fn()),
}));

vi.mock('../../src/ui/backend-client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/ui/backend-client')>();
  return {
    ...actual,
    backendApi: {
      wikiAgentSession: vi.fn(),
      wikiAgentSessions: vi.fn(),
      wikiPage: vi.fn(),
      wikiKBs: vi.fn(),
      wikiInit: vi.fn(),
      wikiPages: vi.fn(),
      wikiSummary: vi.fn(),
      wikiUpload: vi.fn(),
      wikiIngest: vi.fn(),
      sessionTodos: vi.fn(),
      getSessionModel: vi.fn(),
      setSessionModel: vi.fn(),
      deleteSession: vi.fn(async () => ({ ok: true })),
      sessions: vi.fn(async () => []),
      channelSessions: vi.fn(async () => ({ platforms: [] })),
    },
  };
});

vi.mock('../../src/ui/features/session-controller', () => ({
  openSession: mockOpenSession,
  loadBackendHistory: mockLoadBackendHistory,
}));

vi.mock('../../src/ui/ui-feedback', () => ({
  showConfirmDialog: mockShowConfirmDialog,
}));

// ── chat-controller 的重依赖（与 chat-controller-send.test.ts 一致） ──
vi.mock('../../src/ui/features/running-intro', () => ({ syncRunningIntroSlot: vi.fn() }));
vi.mock('../../src/ui/features/usage-tracker', () => ({ recordTurn: vi.fn() }));
vi.mock('../../src/ui/features/cron-page', () => ({ onAfterFinal: vi.fn() }));
vi.mock('../../src/ui/features/kanban-board', () => ({
  refreshKanbanBoard: vi.fn(async () => undefined),
  renderKanbanBoard: vi.fn(),
}));
vi.mock('../../src/ui/features/inspector', () => ({
  isInspectorOpen: vi.fn(() => false),
  openInspectorToTab: vi.fn(),
  refreshInspector: vi.fn(),
  refreshInspectorChrome: vi.fn(),
}));
vi.mock('../../src/ui/features/composer-toolbar', () => ({
  syncComposerModelLabel: vi.fn(),
  syncComposerWorkspaceLabel: vi.fn(),
}));
vi.mock('../../src/ui/features/model-picker', () => ({
  syncModelUi: vi.fn(),
  openModelSelectPopover: mockOpenModelSelectPopover,
}));
vi.mock('../../src/ui/features/system-page', () => ({ renderSystemOverview: vi.fn() }));
vi.mock('../../src/ui/features/attachments', () => ({
  takeAttachmentsForSend: vi.fn(() => []),
  renderAttachmentPreview: vi.fn(),
  renderAttachmentList: vi.fn(),
  bindFilePaste: vi.fn(),
  bindFileDrop: vi.fn(),
}));
vi.mock('../../src/ui/features/session-model', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/ui/features/session-model')>();
  return {
    ...actual,
    persistDraftSessionModel: vi.fn(async () => undefined),
  };
});
const api = backendApi as unknown as {
  wikiAgentSession: ReturnType<typeof vi.fn>;
  wikiAgentSessions: ReturnType<typeof vi.fn>;
  wikiPage: ReturnType<typeof vi.fn>;
  wikiKBs: ReturnType<typeof vi.fn>;
  wikiInit: ReturnType<typeof vi.fn>;
  wikiPages: ReturnType<typeof vi.fn>;
  wikiSummary: ReturnType<typeof vi.fn>;
  wikiUpload: ReturnType<typeof vi.fn>;
  wikiIngest: ReturnType<typeof vi.fn>;
  sessionTodos: ReturnType<typeof vi.fn>;
  getSessionModel: ReturnType<typeof vi.fn>;
  setSessionModel: ReturnType<typeof vi.fn>;
  deleteSession: ReturnType<typeof vi.fn>;
};

const mockSelectFile = vi.fn();

const NOW = Math.floor(Date.now() / 1000);
// 每个用例使用唯一 session id：chat-controller 的 X3b renderTargets 缓存按 session id 失效，
// 跨用例复用同一 id 会让 wrapper 指向已被 innerHTML 清掉的旧 DOM（渲染断言假失败）。
let wikiSidSeq = 0;
let WIKI_SID = 'wiki-sid-0';

function makePage(partial: Partial<WikiPage>): WikiPage {
  return {
    id: 'p1',
    page_type: 'entity',
    title: 'React 笔记',
    file_path: 'dir/a.md',
    sources: [],
    related: [],
    status: 'published',
    tags: [],
    created_at: NOW - 86400,
    updated_at: NOW,
    aliases: [],
    ...partial,
  };
}

type MockSocket = {
  send: ReturnType<typeof vi.fn>;
  planEnter: ReturnType<typeof vi.fn>;
  subscribe: ReturnType<typeof vi.fn>;
  stop: ReturnType<typeof vi.fn>;
};

function socket(): MockSocket {
  return uiStore.get().socket as unknown as MockSocket;
}

/** 打开 Wiki 页面并等待专用 Wiki Agent 会话挂载。 */
async function enterWiki(): Promise<string> {
  uiStore.set({ activeTab: 'wiki' });
  await refreshWikiData();
  await vi.waitFor(() => expect(mockLoadBackendHistory).toHaveBeenCalledWith(WIKI_SID));
  return WIKI_SID;
}

/** 发送一条消息并返回 payload 的 request_id。 */
async function sendAndGetRequestId(text: string): Promise<string> {
  const input = document.querySelector<HTMLTextAreaElement>('[data-wiki-agent-panel] [data-composer-input]')!;
  input.value = text;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  document.querySelector<HTMLButtonElement>('[data-wiki-agent-panel] [data-composer-send]')!.click();
  await vi.waitFor(() => expect(socket().send).toHaveBeenCalled());
  const payload = socket().send.mock.calls.at(-1)?.[0] as { request_id: string };
  return payload.request_id;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockShowConfirmDialog.mockResolvedValue(true);
  __resetAllStoresForTest();
  __resetWikiViewForTest();
  __resetWikiAgentForTest();
  setWikiAgentEntryHandler(null);
  const localStorageStub = {
    getItem: vi.fn(() => null),
    setItem: vi.fn(),
    removeItem: vi.fn(),
    clear: vi.fn(),
  };
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    writable: true,
    value: localStorageStub,
  });
  configStore.set({ configModel: 'test-model' });
  // Composer（主对话本体）未登录会禁用输入/发送；测试默认已登录。
  authStore.set({ isLoggedIn: true });
  workspaceStore.set({
    currentWorkspaceId: 'default',
    workspaces: [{ id: 'default', name: '对话', description: '', instructions: '' }],
  });
  uiStore.set({
    backendConnected: true,
    socket: {
      send: vi.fn(async () => true),
      planEnter: vi.fn(async () => true),
      subscribe: vi.fn(async () => true),
      stop: vi.fn(async () => true),
    } as never,
  });
  mockOpenSession.mockImplementation(async (sid: string) => {
    sessionStore.set({ activeSessionId: sid });
  });
  (window as unknown as { Crew: unknown }).Crew = { selectFile: mockSelectFile };
  document.body.innerHTML = `    <div id="history-list"></div>
    <section id="welcome-panel"></section>
    <section id="chat-panel" hidden>
      <div id="chat-messages"></div>
      <div id="chat-wiki-slot"></div>
      <div class="chat-queue-slot"></div>
      <div class="chat-todo-slot"></div>
      <div id="composer-controls"></div>
      <div class="chat-running-intro"></div>
      <textarea data-composer-input></textarea>
    </section>
    <section id="wiki-tab" class="tab-pane"><div id="wiki-page-root"></div></section>
  `;
  WIKI_SID = `wiki-sid-${++wikiSidSeq}`;
  api.wikiAgentSession.mockResolvedValue({ ok: true, session_id: WIKI_SID });
  api.wikiAgentSessions.mockResolvedValue({
    ok: true,
    kb_id: 'default',
    sessions: [{
      session_id: WIKI_SID,
      title: '默认知识库说明',
      message_count: 2,
      updated_at: NOW,
      workspace_id: 'wiki',
    }],
  });
  api.wikiPage.mockResolvedValue({
    ok: true,
    page: makePage({ content: '**粗体正文**' }),
    source_titles: {},
    source_files: {},
  });
  api.wikiKBs.mockResolvedValue({
    ok: true,
    kbs: [{ id: 'default', name: '默认知识库', created_at: 0, updated_at: 0 }],
  });
  api.wikiInit.mockResolvedValue({ ok: true });
  api.wikiPages.mockResolvedValue({ ok: true, pages: [], source_titles: {}, source_files: {} });
  api.wikiSummary.mockResolvedValue({ ok: true, summary: '', kb_id: 'default', status: 'empty' });
  api.sessionTodos.mockResolvedValue({ ok: true, todos: [] });
  api.getSessionModel.mockResolvedValue({ ok: true, model_profile_id: 'glm-fast', model_label: 'GLM 快速' });
  api.setSessionModel.mockImplementation(async (_sid: string, id: string) => ({
    ok: true,
    model_profile_id: id,
    model_label: `模型-${id}`,
  }));
  initWikiAgent();
});

// ── Wiki Agent 结果卡片渲染 ──

describe('wiki_cards 渲染', () => {
  it('wiki_cards 挂到本回合 assistant 消息并渲染卡片网格（含查看按钮）', async () => {
    await enterWiki();
    const rid = await sendAndGetRequestId('查一下');

    applyChunk({
      kind: 'final',
      body: { text: '这是答案' },
      session_id: WIKI_SID,
      request_id: rid,
      sequence: 2,
      is_final: true,
    } as ChatChunk);
    applyChunk({
      kind: 'wiki_cards',
      body: { pages: [makePage({ id: 'p1', title: 'React 笔记', summary: 'Hooks 用法', tags: ['前端'] })] },
      session_id: WIKI_SID,
      request_id: rid,
      sequence: 3,
      is_final: false,
    } as ChatChunk);

    const msgs = messageStore.get().messages[WIKI_SID] ?? [];
    const assistant = [...msgs].reverse().find((m) => m.role === 'assistant');
    expect(assistant?.wikiCards?.[0]?.title).toBe('React 笔记');

    await vi.waitFor(() => expect(document.querySelector('.wiki-cards-panel')).not.toBeNull());
    const panel = document.querySelector('.wiki-cards-panel');
    expect(panel?.textContent).toContain('Wiki 结果');
    expect(panel?.textContent).toContain('React 笔记');
    expect(panel?.textContent).toContain('Hooks 用法');
    expect(panel?.textContent).toContain('前端');
    const viewBtn = document.querySelector('[data-wiki-view-page]');
    expect(viewBtn?.getAttribute('data-wiki-view-page')).toBe('p1');
    expect(viewBtn?.textContent).toBe('查看');
  });

  it('点卡片「查看」打开详情时不动左侧对话（DOM 与滚动位置保持）', async () => {
    await enterWiki();
    const rid = await sendAndGetRequestId('查一下');
    applyChunk({
      kind: 'final',
      body: { text: '这是答案' },
      session_id: WIKI_SID,
      request_id: rid,
      sequence: 2,
      is_final: true,
    } as ChatChunk);
    applyChunk({
      kind: 'wiki_cards',
      body: { pages: [makePage({ id: 'p1', title: 'React 笔记', summary: 'Hooks 用法' })] },
      session_id: WIKI_SID,
      request_id: rid,
      sequence: 3,
      is_final: false,
    } as ChatChunk);
    await vi.waitFor(() => expect(document.querySelector('[data-wiki-view-page]')).not.toBeNull());

    const messages = document.querySelector<HTMLElement>('[data-wiki-agent-messages]')!;
    const cardsPanel = messages.querySelector('.wiki-cards-panel');
    // 记录滚动写入：谁动了 scrollTop 一览无遗
    const scrollWrites: number[] = [];
    let scrollValue = 456;
    Object.defineProperty(messages, 'scrollTop', {
      get: () => scrollValue,
      set: (v: number) => { scrollWrites.push(v); scrollValue = v; },
      configurable: true,
    });

    (document.querySelector('[data-wiki-view-page]') as HTMLElement).click();
    await vi.waitFor(() => expect(api.wikiPage).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 50));

    expect(messages.isConnected).toBe(true);
    expect(messages.querySelector('.wiki-cards-panel')).toBe(cardsPanel);
    // renderShell 重建时短暂 detach 会重置浏览器滚动位置,修复后应恢复为记住的值
    expect(scrollWrites).toContain(456);
    expect(scrollValue).toBe(456);
  });

  it('旧 request_id 的迟到 wiki_cards 被 turn gate 丢弃', async () => {
    await enterWiki();
    const rid = await sendAndGetRequestId('查一下');
    applyChunk({
      kind: 'final',
      body: { text: '这是答案' },
      session_id: WIKI_SID,
      request_id: rid,
      sequence: 2,
      is_final: true,
    } as ChatChunk);
    applyChunk({
      kind: 'wiki_cards',
      body: { pages: [makePage({})] },
      session_id: WIKI_SID,
      request_id: 'req-stale',
      sequence: 3,
      is_final: false,
    } as ChatChunk);
    const msgs = messageStore.get().messages[WIKI_SID] ?? [];
    expect(msgs.every((m) => !m.wikiCards)).toBe(true);
  });

});

// ── 上传失败反向 prompt ──

describe('反向注入 prompt', () => {
  it('buildWikiAssistPrompt 携带文件名 + 错误 + source_id', () => {
    expect(buildWikiAssistPrompt({ fileName: 'a.md', error: '解析失败', sourceId: 's1' })).toBe(
      '我上传「a.md」到 Wiki 时处理失败，错误信息：解析失败。请帮我分析原因并重新处理这个文件（source_id: s1）。',
    );
    expect(buildWikiAssistPrompt({ fileName: 'a.md', error: '解析失败' })).toBe(
      '我上传「a.md」到 Wiki 时处理失败，错误信息：解析失败。请帮我分析原因并重新处理这个文件。',
    );
  });

  it('在 Wiki 页面注入 assist 后自动发送挽救 prompt（带 wiki 参数）', async () => {
    await enterWiki();
    socket().send.mockClear();
    await openWikiAgent({
      kbId: 'default',
      kbName: '默认知识库',
      assist: { fileName: 'a.md', error: '解析失败', sourceId: 's1' },
    });
    await vi.waitFor(() => expect(socket().send).toHaveBeenCalled());
    const payload = socket().send.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(String(payload.query)).toContain('a.md');
    expect(String(payload.query)).toContain('解析失败');
    expect(String(payload.query)).toContain('source_id: s1');
    expect(payload.wiki_kb_id).toBe('default');
  });
});

// ── wiki-page 入口挂点 ──

describe('wiki-page 入口挂点', () => {
  it('Wiki Composer 的发送/停止按钮显隐走 Composer 全局 [hidden] 规则', () => {
    // 重构后 Wiki 对话用主对话 Composer 本体（mw-composer），按钮显隐由 composer.css 统一承担。
    const css = readFileSync('assets/styles/composer.css', 'utf8');
    expect(css).toMatch(/\.mw-composer\s+\[hidden\]\s*\{\s*display:\s*none;/);
  });

  it('Wiki 页面渲染三栏右侧 Agent，并在当前页发送到按 KB 隔离的会话', async () => {
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();

    await vi.waitFor(() => {
      expect(document.querySelector('[data-wiki-agent-panel]')).not.toBeNull();
      expect(mockLoadBackendHistory).toHaveBeenCalledWith(WIKI_SID);
    });
    const panel = document.querySelector<HTMLElement>('[data-wiki-agent-panel]')!;
    const messages = panel.querySelector<HTMLElement>('[data-wiki-agent-messages]')!;
    // 面板头常驻：标题（含 KB 名）+ 新建/历史/展开按钮；无旧版 status 行
    expect(panel.querySelector('.wiki-agent-pane__header')).not.toBeNull();
    expect(panel.querySelector('.wiki-agent-pane__title')?.textContent).toContain('默认知识库');
    expect(panel.querySelector('[data-wiki-agent-new]')).not.toBeNull();
    expect(panel.querySelector('[data-wiki-agent-history]')).not.toBeNull();
    expect(panel.querySelector('[data-wiki-agent-expand]')).not.toBeNull();
    expect(panel.querySelector('[data-wiki-agent-status]')).toBeNull();
    expect(messages.classList.contains('chat-messages')).toBe(true);
    expect(messages.classList.contains('web-flow')).toBe(true);
    expect(panel.textContent).not.toContain('已连接');
    expect(panel.querySelector<HTMLTextAreaElement>('[data-composer-input]')?.placeholder)
      .toBe('基于知识库提问');
    // 空态标语 + 底部免责声明
    await vi.waitFor(() => {
      expect(panel.querySelector('.wiki-agent-pane__void')?.textContent).toContain('基于知识库问答');
    });
    expect(panel.querySelector('.wiki-agent-pane__disclaimer')?.textContent).toContain('内容由 AI 生成');
    expect(api.wikiAgentSession).toHaveBeenCalledWith('default');
    expect(mockOpenSession).not.toHaveBeenCalled();

    const input = document.querySelector<HTMLTextAreaElement>('[data-wiki-agent-panel] [data-composer-input]')!;
    input.value = '总结当前知识库';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLButtonElement>('[data-wiki-agent-panel] [data-composer-send]')!.click();

    await vi.waitFor(() => expect(socket().send).toHaveBeenCalled());
    const payload = socket().send.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(payload.session_id).toBe(WIKI_SID);
    expect(payload.query).toBe('总结当前知识库');
    expect(payload.wiki_kb_id).toBe('default');
    expect(uiStore.get().activeTab).toBe('wiki');
  });

  it('面板内消息复制按钮可用（不依赖 #chat-messages 的全局委托）', async () => {
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();
    await vi.waitFor(() => expect(mockLoadBackendHistory).toHaveBeenCalledWith(WIKI_SID));

    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });

    const messages = document.querySelector<HTMLElement>('[data-wiki-agent-messages]')!;
    messages.innerHTML = '<button type="button" class="chat-copy-btn" data-copy="复制这段">复制</button>';
    messages.querySelector<HTMLButtonElement>('.chat-copy-btn')!.click();

    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith('复制这段'));
  });

  it('历史对话浮层支持删除非当前会话', async () => {
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();
    await vi.waitFor(() => expect(mockLoadBackendHistory).toHaveBeenCalledWith(WIKI_SID));

    api.wikiAgentSessions.mockResolvedValue({
      ok: true,
      kb_id: 'default',
      sessions: [
        { session_id: WIKI_SID, title: '当前会话', updated_at: NOW, message_count: 2 },
        { session_id: 'wiki-old-1', title: '旧会话', updated_at: NOW - 60, message_count: 1 },
      ],
    });

    document.querySelector<HTMLButtonElement>('[data-wiki-agent-history]')!.click();
    await vi.waitFor(() => {
      expect(document.querySelector('[data-wiki-agent-history-delete="wiki-old-1"]')).not.toBeNull();
    });

    document.querySelector<HTMLButtonElement>('[data-wiki-agent-history-delete="wiki-old-1"]')!.click();

    await vi.waitFor(() => expect(api.deleteSession).toHaveBeenCalledWith('wiki-old-1'));
    expect(mockShowConfirmDialog).toHaveBeenCalled();
    // 删的不是当前会话：不触发切换/新建。
    expect(api.wikiAgentSession).not.toHaveBeenCalledWith('default', { forceNew: true });
  });

  it('删除 KB 后清理内嵌会话缓存，同名 KB 会重新向后端取会话', async () => {
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();
    await vi.waitFor(() => expect(mockLoadBackendHistory).toHaveBeenCalledWith(WIKI_SID));

    const freshSid = 'wiki-fresh-session';
    forgetWikiAgentKb('default');
    api.wikiAgentSession.mockResolvedValue({
      ok: true,
      session_id: freshSid,
      kb_id: 'default',
    });
    renderWikiPage();

    await vi.waitFor(() => {
      expect(api.wikiAgentSession).toHaveBeenLastCalledWith('default');
      expect(mockLoadBackendHistory).toHaveBeenCalledWith(freshSid);
    });
  });

  it('切换知识库时立即切换右栏会话，消息和发送目标不串库', async () => {
    const otherSessionId = `wiki-other-${wikiSidSeq}`;
    api.wikiKBs.mockResolvedValue({
      ok: true,
      kbs: [
        { id: 'default', name: '默认知识库', created_at: 0, updated_at: 0 },
        { id: 'other', name: '新知识库', created_at: 0, updated_at: 0 },
      ],
    });
    api.wikiAgentSession.mockImplementation(async (kbId: string) => ({
      ok: true,
      session_id: kbId === 'other' ? otherSessionId : WIKI_SID,
      kb_id: kbId,
    }));

    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();
    await vi.waitFor(() => expect(mockLoadBackendHistory).toHaveBeenCalledWith(WIKI_SID));
    appendMessage(WIKI_SID, 'assistant', '只属于默认知识库的旧消息');
    await vi.waitFor(() => {
      expect(document.querySelector('[data-wiki-agent-messages]')?.textContent)
        .toContain('只属于默认知识库的旧消息');
    });

    const select = document.querySelector<HTMLSelectElement>('#wiki-kb-select')!;
    select.value = 'other';
    select.dispatchEvent(new Event('change'));

    await vi.waitFor(() => {
      expect(document.querySelector('[data-wiki-agent-panel]')?.getAttribute('data-kb-id')).toBe('other');
      expect(mockLoadBackendHistory).toHaveBeenCalledWith(otherSessionId);
      expect(document.querySelector('.wiki-agent-pane__title')?.textContent).toContain('新知识库');
    });
    expect(document.querySelector('[data-wiki-agent-messages]')?.textContent)
      .not.toContain('只属于默认知识库的旧消息');

    const input = document.querySelector<HTMLTextAreaElement>('[data-wiki-agent-panel] [data-composer-input]')!;
    input.value = '查询新知识库';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLButtonElement>('[data-wiki-agent-panel] [data-composer-send]')!.click();
    await vi.waitFor(() => expect(socket().send).toHaveBeenCalled());
    const payload = socket().send.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(payload.session_id).toBe(otherSessionId);
    expect(payload.wiki_kb_id).toBe('other');
  });

  it('右栏 Composer 接线粘贴 / 拖拽上传（复用 attachments 通用绑定）', async () => {
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();
    await vi.waitFor(() => expect(document.querySelector('[data-wiki-agent-panel] [data-composer-input]')).not.toBeNull());

    const input = document.querySelector<HTMLTextAreaElement>('[data-wiki-agent-panel] [data-composer-input]')!;
    const panel = document.querySelector<HTMLElement>('[data-wiki-agent-panel]')!;
    expect(vi.mocked(bindFilePaste)).toHaveBeenCalledWith(input, expect.any(Function));
    // 拖拽热区是整个问答面板（拖到消息区也能传），且重复挂载不重复绑定
    expect(vi.mocked(bindFileDrop)).toHaveBeenCalledWith(panel, expect.any(Function));
  });

  it('运行中只显示停止按钮，点击后停止当前 Wiki 会话并恢复发送按钮', async () => {
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();
    await vi.waitFor(() => expect(document.querySelector('[data-wiki-agent-panel] [data-composer-send]')).not.toBeNull());

    setBusy(WIKI_SID, true);
    await vi.waitFor(() => {
      expect(document.querySelector<HTMLButtonElement>('[data-wiki-agent-panel] [data-composer-send]')?.hidden).toBe(true);
      expect(document.querySelector<HTMLButtonElement>('[data-wiki-agent-panel] [data-composer-stop]')?.hidden).toBe(false);
    });

    document.querySelector<HTMLButtonElement>('[data-wiki-agent-panel] [data-composer-stop]')!.click();
    expect(socket().stop).toHaveBeenCalledWith(WIKI_SID);
    await vi.waitFor(() => {
      expect(document.querySelector<HTMLButtonElement>('[data-wiki-agent-panel] [data-composer-send]')?.hidden).toBe(false);
      expect(document.querySelector<HTMLButtonElement>('[data-wiki-agent-panel] [data-composer-stop]')?.hidden).toBe(true);
    });
  });

  it('Wiki 会话显示 Todo 卡片，并渲染 ask_followup_question 后把答案发回同一会话', async () => {
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();
    await vi.waitFor(() => {
      expect(document.querySelector('[data-wiki-agent-panel]')).not.toBeNull();
      expect(api.sessionTodos).toHaveBeenCalledWith(WIKI_SID);
    });

    setBookTodos(WIKI_SID, [
      { id: 'todo-1', content: '解析附件', status: 'in_progress' },
      { id: 'todo-2', content: '整理知识页面', status: 'pending' },
    ]);
    patchBook(WIKI_SID, {
      pendingFollowup: {
        questionId: 'followup-1',
        title: '需要确认',
        recordHistory: true,
        questions: [{
          id: 'scope',
          question: '要整理哪些内容？',
          options: [{ label: '全部内容', value: 'all' }],
          allowFreeText: false,
          multiSelect: false,
        }],
      },
    });

    await vi.waitFor(() => {
      expect(document.querySelector('[data-wiki-agent-panel] .chat-todo-slot')?.textContent).toContain('解析附件');
      expect(document.querySelector('.followup-card')?.textContent).toContain('要整理哪些内容？');
    });
    const option = document.querySelector<HTMLInputElement>('.followup-card input[value="all"]')!;
    option.checked = true;
    option.dispatchEvent(new Event('change', { bubbles: true }));
    document.querySelector<HTMLButtonElement>('.followup-card__submit')!.click();

    expect(socket().send).toHaveBeenLastCalledWith({
      action: 'followup_answer',
      session_id: WIKI_SID,
      question_id: 'followup-1',
      answers: [{ question_id: 'scope', answers: ['all'] }],
    });
    expect(sessionStore.get().books[WIKI_SID]?.pendingFollowup).toBeNull();
    expect(messageStore.get().messages[WIKI_SID]?.find((message) => message.role === 'status')?.content)
      .toBe('已选择：全部内容');
    expect(messageStore.get().messages[WIKI_SID]?.some((message) => (
      message.role === 'user' && message.content === '已选择：全部内容'
    ))).toBe(false);
  });

  it('右栏「已编辑文件」卡「查看」：跳主聊天区同一会话并展开 Files 看板定位文件', async () => {
    const mockSetTab = vi.fn();
    setChatCallbacks({ openSession: mockOpenSession, setTab: mockSetTab });
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();
    await vi.waitFor(() => expect(document.querySelector('[data-wiki-agent-panel]')).not.toBeNull());

    appendMessage(WIKI_SID, 'assistant', '已生成文件', {
      turnFileChanges: [{ path: '/tmp/wiki-out.md', added: 13, removed: 0, status: 'added' }],
    });

    await vi.waitFor(() => {
      expect(document.querySelector('.msg__file-changes__review')).not.toBeNull();
    });
    document.querySelector<HTMLButtonElement>('.msg__file-changes__review')!.click();

    await vi.waitFor(() => {
      expect(mockOpenSession).toHaveBeenCalledWith(WIKI_SID);
      expect(mockSetTab).toHaveBeenCalledWith('chat');
      expect(vi.mocked(openInspectorToTab)).toHaveBeenCalledWith('files', { expandFilePath: '/tmp/wiki-out.md' });
    });
  });

  it('新建对话创建独立 Wiki session，保留旧会话并切到空白输入', async () => {
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();
    await vi.waitFor(() => expect(document.querySelector('[data-wiki-agent-new]')).not.toBeNull());

    const newSessionId = `wiki-new-${wikiSidSeq}`;
    api.wikiAgentSession.mockResolvedValueOnce({ ok: true, session_id: newSessionId, kb_id: 'default' });
    document.querySelector<HTMLElement>('[data-wiki-agent-new]')!.click();

    await vi.waitFor(() => {
      expect(api.wikiAgentSession).toHaveBeenLastCalledWith('default', { forceNew: true });
      expect(mockLoadBackendHistory).toHaveBeenCalledWith(newSessionId);
    });
    expect(sessionStore.get().sessions.some((session) => session.id === WIKI_SID)).toBe(true);
    expect(sessionStore.get().sessions.some((session) => session.id === newSessionId)).toBe(true);
    expect(document.querySelector<HTMLTextAreaElement>('[data-wiki-agent-panel] [data-composer-input]')?.value).toBe('');
  });

  it('历史按钮只列当前 KB 会话，点击后切换并恢复对应历史', async () => {
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();
    await vi.waitFor(() => expect(document.querySelector('[data-wiki-agent-history]')).not.toBeNull());

    const oldSessionId = `wiki-old-${wikiSidSeq}`;
    api.sessionTodos.mockImplementation(async (sessionId: string) => ({
      ok: true,
      todos: sessionId === oldSessionId
        ? [{ id: 'old-todo', content: '旧会话待办', status: 'in_progress' }]
        : [],
    }));
    api.wikiAgentSessions.mockResolvedValueOnce({
      ok: true,
      kb_id: 'default',
      sessions: [
        { session_id: WIKI_SID, title: '当前对话', message_count: 2, updated_at: NOW, workspace_id: 'wiki' },
        { session_id: oldSessionId, title: '旧对话', message_count: 6, updated_at: NOW - 3600, workspace_id: 'wiki' },
      ],
    });
    document.querySelector<HTMLElement>('[data-wiki-agent-history]')!.click();

    await vi.waitFor(() => {
      expect(api.wikiAgentSessions).toHaveBeenCalledWith('default');
      expect(document.querySelector('[data-wiki-agent-history-popover]')?.textContent).toContain('旧对话');
    });
    document.querySelector<HTMLElement>(`[data-wiki-agent-history-session="${oldSessionId}"]`)!.click();

    await vi.waitFor(() => {
      expect(mockLoadBackendHistory).toHaveBeenCalledWith(oldSessionId);
      expect(api.sessionTodos).toHaveBeenCalledWith(oldSessionId);
      expect(api.getSessionModel).toHaveBeenCalledWith(oldSessionId);
      expect(document.querySelector('[data-wiki-agent-panel] .chat-todo-slot')?.textContent).toContain('旧会话待办');
    });
    expect(document.querySelector<HTMLElement>('[data-wiki-agent-history-popover]')?.hidden).toBe(true);
  });

  it('模型 chip：加载会话模型绑定，选择后走会话级接口切换（不影响主对话）', async () => {
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();

    // 绑定加载后 chip 显示后端返回的模型名
    await vi.waitFor(() => {
      expect(document.querySelector('[data-wiki-agent-model-label]')?.textContent).toBe('GLM 快速');
    });
    expect(api.getSessionModel).toHaveBeenCalledWith(WIKI_SID);

    // 点击 chip 打开模型浮层（复用 model-picker 的通用浮层），高亮当前绑定
    document.querySelector<HTMLElement>('[data-wiki-agent-model]')!.click();
    expect(mockOpenModelSelectPopover).toHaveBeenCalledTimes(1);
    const opts = mockOpenModelSelectPopover.mock.calls[0]?.[0] as {
      activeId: string;
      onPick: (id: string) => void;
    };
    expect(opts.activeId).toBe('glm-fast');

    // 选择新模型 → PUT 会话级接口（workspace=wiki）→ chip 更新
    opts.onPick('minimax-m3');
    await vi.waitFor(() => {
      expect(document.querySelector('[data-wiki-agent-model-label]')?.textContent).toBe('模型-minimax-m3');
    });
    expect(api.setSessionModel).toHaveBeenCalledWith(WIKI_SID, 'minimax-m3', { workspace_id: 'wiki' });
    expect(mockOpenSession).not.toHaveBeenCalled();
  });

  it('面板头按钮收起/展开知识库面板，renderShell 重建后面板活节点保留', async () => {
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();
    await vi.waitFor(() => expect(document.querySelector('[data-wiki-agent-expand]')).not.toBeNull());

    const panel = document.querySelector<HTMLElement>('[data-wiki-agent-panel]')!;
    expect(document.querySelector('.page-shell--wiki')?.classList.contains('wiki-browser-collapsed')).toBe(false);

    document.querySelector<HTMLElement>('[data-wiki-agent-expand]')!.click();
    expect(document.querySelector('.page-shell--wiki')?.classList.contains('wiki-browser-collapsed')).toBe(true);

    // wiki 页重渲染保留面板活节点（KB 未变）：按钮仍在同一面板上
    const rebuilt = document.querySelector<HTMLElement>('[data-wiki-agent-panel]')!;
    expect(rebuilt).toBe(panel);

    rebuilt.querySelector<HTMLElement>('[data-wiki-agent-expand]')!.click();
    expect(document.querySelector('.page-shell--wiki')?.classList.contains('wiki-browser-collapsed')).toBe(false);
  });

  it('对话面板为弹性主区：无独立宽度手柄与内联宽度', async () => {
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();
    await vi.waitFor(() => expect(document.querySelector('[data-wiki-agent-panel]')).not.toBeNull());

    const panel = document.querySelector<HTMLElement>('[data-wiki-agent-panel]')!;
    expect(panel.style.width).toBe('');
    expect(document.querySelector('[data-wiki-agent-sash]')).toBeNull();
  });

  it('输入草稿在 renderShell 后保留', async () => {
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();
    await vi.waitFor(() => expect(document.querySelector('[data-wiki-agent-panel] [data-composer-input]')).not.toBeNull());

    const input = document.querySelector<HTMLTextAreaElement>('[data-wiki-agent-panel] [data-composer-input]')!;
    input.value = '写到一半的草稿';
    input.dispatchEvent(new Event('input', { bubbles: true }));

    renderWikiPage();
    const rebuilt = document.querySelector<HTMLTextAreaElement>('[data-wiki-agent-panel] [data-composer-input]')!;
    expect(rebuilt).toBe(input);
    expect(rebuilt.value).toBe('写到一半的草稿');
  });

  it('IME 合成中按 Enter 不发送，普通 Enter 发送', async () => {
    uiStore.set({ activeTab: 'wiki' });
    await refreshWikiData();
    await vi.waitFor(() => expect(document.querySelector('[data-wiki-agent-panel] [data-composer-input]')).not.toBeNull());

    const input = document.querySelector<HTMLTextAreaElement>('[data-wiki-agent-panel] [data-composer-input]')!;
    input.value = '你好';
    // 让 Composer 感知草稿（hasDraft → 发送按钮可用），与真实输入路径一致。
    input.dispatchEvent(new Event('input', { bubbles: true }));

    // 中文输入法选字 Enter：isComposing=true，不应触发发送
    const imeEnter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    Object.defineProperty(imeEnter, 'isComposing', { value: true });
    input.dispatchEvent(imeEnter);
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(socket().send).not.toHaveBeenCalled();
    expect(input.value).toBe('你好');

    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(socket().send).toHaveBeenCalled());
    const payload = socket().send.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(payload.query).toBe('你好');
  });
});

// ── reducer 纯函数 ──

describe('wiki reducer（纯函数）', () => {
  function snap(overrides: Partial<ReducerSnapshot> = {}): ReducerSnapshot {
    return {
      sessionId: WIKI_SID,
      messages: [],
      book: {
        toolMap: new Map(),
        assistantId: null,
        firstChunkAt: null,
        planActive: false,
        pendingPlan: null,
        pendingFollowup: null,
        todos: [],
        fileChanges: [],
        deltaSpans: [],
        legacyDeltaText: '',
        turnSealed: false,
        activeRequestId: null,
        acceptingNewRequest: false,
      } as Bookkeeping,
      currentStatus: 'idle',
      now: 1_700_000_000_000,
      sequence: 1,
      ...overrides,
    };
  }

  it('normalizeWikiCardPages 兼容 pages/cards 字段并补默认值', () => {
    const fromPages = normalizeWikiCardPages({ pages: [{ id: 'p1', title: 'A' }] });
    expect(fromPages.length).toBe(1);
    expect(fromPages[0]).toMatchObject({ id: 'p1', title: 'A', page_type: 'entity', status: 'published', tags: [] });
    const fromCards = normalizeWikiCardPages({ cards: [{ id: 'p2', title: 'B', page_type: 'entity' }] });
    expect(fromCards[0]).toMatchObject({ id: 'p2', page_type: 'entity' });
    expect(normalizeWikiCardPages({})).toEqual([]);
  });

  it('wiki_cards patch 到最后一条 assistant；无 assistant 时新建空载体消息', () => {
    const existing = {
      id: 'm-1',
      role: 'assistant' as const,
      content: '答案',
      timestamp: 1,
    };
    const chunk = normalizeChunk({
      kind: 'wiki_cards',
      body: { pages: [{ id: 'p1', title: 'A' }] },
      sequence: 9,
    }) as AnyChatChunk;
    const patched = reduceChunk(chunk, snap({ messages: [existing] }));
    expect(patched.messageUpserts).toHaveLength(1);
    expect(patched.messageUpserts[0]?.op).toBe('patch');
    expect(patched.messageUpserts[0]?.messageId).toBe('m-1');
    expect(patched.messageUpserts[0]?.patch?.wikiCards?.[0]?.id).toBe('p1');

    const appended = reduceChunk(chunk, snap());
    expect(appended.messageUpserts[0]?.op).toBe('append');
    expect(appended.messageUpserts[0]?.message?.wikiCards?.[0]?.id).toBe('p1');
    // 附属帧不触碰 busy / 状态
    expect(appended.statusHint).toBeUndefined();
    expect(appended.finalize).toBe(false);
  });

  it('wiki_cards 空数组 no-op', () => {
    const chunk = normalizeChunk({ kind: 'wiki_cards', body: { pages: [] }, sequence: 9 }) as AnyChatChunk;
    const result = reduceChunk(chunk, snap());
    expect(result.messageUpserts).toEqual([]);
  });

});
