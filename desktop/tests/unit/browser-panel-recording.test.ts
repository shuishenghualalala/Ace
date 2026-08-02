// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest';

import { backendApi, type BrowserPageState } from '../../src/ui/backend-client';
import {
  addRecordingNote,
  browserRecordingState,
  closeRecordingNoteComposer,
  compileLastRecording,
  discardLastRecording,
  openRecordingNoteComposer,
  openUserBrowser,
  renderBrowserPanel,
  renderBrowserRecordingBar,
  sendRecordingControl,
  syncBrowserPanelSession,
} from '../../src/ui/features/browser-panel';
import { setActiveSessionId } from '../../src/ui/state';

function pageState(overrides: Partial<BrowserPageState> = {}): BrowserPageState {
  return {
    owner_hash: 'owner', session_hash: 'session', tab_id: 'tab-1',
    tab_label: 'session-tab-1', url: 'https://example.com/', title: '',
    generation: 0, mode: 'human', running: false, last_action: '', last_error: '',
    screenshot_id: '', viewport_width: 0, viewport_height: 0,
    can_go_back: false, can_go_forward: false, tabs: [], downloads: [],
    ...overrides,
  };
}

function recordingResponse(recording: boolean, paused: boolean, steps: number) {
  return {
    ok: true,
    state: pageState(),
    // recording_id 必须在：生产后端一定返回它，而删除分支要靠它才会执行。
    // 早先 mock 漏了这个字段，`discardLastRecording` 一进去就 early-return，
    // 于是「生成技能顺手把轨迹删了」这个回归在单测里完全看不见。
    // Legacy/newer mismatched gateways may still include an absolute trace_dir.
    // The renderer must ignore it even when it is present on the wire.
    recording: {
      recording, paused, steps, recording_id: 'abcd1234', trace_dir: '/tmp/private-owner/trace',
    },
  };
}

/**
 * 让面板真的持有一个已打开的页面，再开始录制。
 *
 * 录制的前置条件就是有一个真实页面：空白标签页的原生 view 是分离的、没有视口，
 * 宿主合成不了第一步 openPage，所以空白页按下录制只会进入**预备**态。这些用例
 * 测的是录制本身，必须先把这个前置条件摆好——和生产里一样。
 */
async function activateRecordingSession(sessionId: string): Promise<void> {
  setActiveSessionId(sessionId);
  vi.spyOn(backendApi, 'browserState').mockResolvedValue({
    ok: true,
    state: pageState({ tab_id: 'tab-1', url: 'https://example.com/', mode: 'human' }),
  });
  await openUserBrowser();
}

describe('浏览器录制指示条', () => {
  afterEach(async () => {
    // 每个用例结束时把录制态复位，避免模块级状态串味
    vi.spyOn(backendApi, 'browserControl').mockResolvedValue(recordingResponse(false, false, 0));
    await sendRecordingControl('stop');
    discardLastRecording();
    syncBrowserPanelSession(null);
    setActiveSessionId(null);
    document.body.innerHTML = '';
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('录制期间步数靠轮询刷新，不能只等 state 事件', async () => {
    // 原来 refreshRecordingSteps 只在收到 `state` 事件时调用，依据是「页面每有变化
    // 就会来一条 state」——这对录制是错的：state 只带 url/title/tabs/mode，而录制记的
    // 是点击/输入/悬停这些页面内动作，一个 state 字段都不改。结果计数永远停在开录
    // 那一刻，用户以为根本没录上。后端为此把 record_status 做成不经宿主的纯读，
    // 注释明写「会被频繁调用」——轮询本就是设计意图，只是渲染层没接上定时器。
    document.body.innerHTML = renderBrowserPanel();
    await activateRecordingSession('session-poll');
    // 定时器必须在开录**之前**换成假的，否则轮询是用真定时器建的，推进不了
    vi.useFakeTimers();
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValue(recordingResponse(true, false, 1));
    await sendRecordingControl('start');
    expect(
      document.querySelector<HTMLElement>('[data-browser-rec-count]')?.textContent,
    ).toContain('1 步');

    // 页面内动作不产生 state 事件，只有轮询能发现步数涨了
    control.mockResolvedValue(recordingResponse(true, false, 7));
    await vi.advanceTimersByTimeAsync(1100);

    expect(control).toHaveBeenCalledWith('session-poll', 'record_status');
    expect(browserRecordingState().steps).toBe(7);
    expect(
      document.querySelector<HTMLElement>('[data-browser-rec-count]')?.textContent,
    ).toContain('7 步');
  });

  it('未录制时指示条隐藏', () => {
    document.body.innerHTML = renderBrowserPanel();
    const bar = document.querySelector<HTMLElement>('[data-browser-recording]');
    expect(bar).not.toBeNull();
    expect(bar?.hidden).toBe(true);
  });

  it('开始录制后显示持续可见的指示与步数', async () => {
    document.body.innerHTML = renderBrowserPanel();
    await activateRecordingSession('session-rec');
    // 已经是 human 模式，start 不需要再走接管
    vi.spyOn(backendApi, 'browserControl').mockResolvedValue(recordingResponse(true, false, 4));

    await sendRecordingControl('start');

    expect(browserRecordingState()).toEqual({ recording: true, paused: false, steps: 4 });
    // 录制中的状态与控件都在工具栏的控件组里，**不再单独占一整行**：
    // 录制贯穿整段演示，长期状态常驻一行纯属浪费面板高度。
    const controls = document.querySelector<HTMLElement>('[data-browser-rec-controls]');
    const group = controls?.querySelector<HTMLElement>('.browser-rec');
    expect(group).not.toBeNull();
    expect(group?.textContent).toContain('正在录制技能');
    expect(group?.textContent).toContain('4 步');
    expect(document.querySelector<HTMLElement>('[data-browser-recording]')?.hidden).toBe(true);
    // 暂停按钮必须在——登录、输密码这类片段要能随时掐掉
    expect(group?.querySelector('[data-browser-record="pause"]')).not.toBeNull();
    expect(group?.querySelector('[data-browser-record="stop"]')).not.toBeNull();
    // 图标按钮必须自带提示，否则用户只能靠猜
    expect(group?.querySelector('[data-browser-record="stop"]')?.getAttribute('title')).toBeTruthy();
    expect(group?.querySelector('[data-browser-record="pause"]')?.getAttribute('aria-label')).toBeTruthy();
  });

  it('暂停态用文字与静止圆点双通道表达，不只靠颜色', async () => {
    document.body.innerHTML = renderBrowserPanel();
    await activateRecordingSession('session-rec');
    vi.spyOn(backendApi, 'browserControl').mockResolvedValue(recordingResponse(true, true, 7));

    await sendRecordingControl('pause');

    const group = document.querySelector<HTMLElement>('[data-browser-rec-controls] .browser-rec');
    expect(group?.textContent).toContain('录制已暂停');
    // 暂停要在颜色之外还有可判定的状态标记（圆点停脉动由 is-paused 驱动）
    expect(group?.classList.contains('is-paused')).toBe(true);
    expect(group?.querySelector('.browser-rec__dot')).not.toBeNull();
    // 暂停时给的是「继续」，不是再一个「暂停」
    expect(group?.querySelector('[data-browser-record="resume"]')).not.toBeNull();
  });

  it('停止录制后录制控件收起，换成生成技能入口', async () => {
    document.body.innerHTML = renderBrowserPanel();
    await activateRecordingSession('session-rec');
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValue(recordingResponse(true, false, 2));
    await sendRecordingControl('start');
    expect(document.querySelector('[data-browser-rec-controls] .browser-rec')).not.toBeNull();

    control.mockResolvedValue(recordingResponse(false, false, 0));
    await sendRecordingControl('stop');

    expect(browserRecordingState().recording).toBe(false);
    // 录制中的控件必须消失——红点还亮着会让用户以为仍在录
    expect(document.querySelector('[data-browser-rec-controls] .browser-rec')).toBeNull();
    expect(document.querySelector('[data-browser-record="pause"]')).toBeNull();
    expect(document.querySelector('.browser-rec__dot')).toBeNull();
    // 录制按钮回到「可以再录一段」的初始形态
    expect(document.querySelector('[data-browser-record="start"]')).not.toBeNull();
    // 但录到的东西要有去处（零步的情况由另一条用例覆盖）
    const bar = document.querySelector<HTMLElement>('[data-browser-recording]');
    expect(bar?.textContent).not.toContain('正在录制');
    expect(bar?.querySelector('[data-browser-record="compile"]')).not.toBeNull();
  });

  it('后端失败时不谎报录制已开始', async () => {
    // 状态必须反映后端真实结果。谎报「正在录制」比不录更糟：用户以为录上了，
    // 演示完却发现什么都没有。
    document.body.innerHTML = renderBrowserPanel();
    await activateRecordingSession('session-rec');
    vi.spyOn(backendApi, 'browserControl').mockRejectedValue(new Error('gateway down'));

    await sendRecordingControl('start');

    expect(browserRecordingState().recording).toBe(false);
    expect(document.querySelector<HTMLElement>('[data-browser-recording]')?.hidden).toBe(true);
  });

  it('停止后留下「生成技能」入口，带步数', async () => {
    document.body.innerHTML = renderBrowserPanel();
    await activateRecordingSession('session-rec');
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValue(recordingResponse(true, false, 9));
    await sendRecordingControl('start');

    control.mockResolvedValue(recordingResponse(false, false, 0));
    await sendRecordingControl('stop');

    const bar = document.querySelector<HTMLElement>('[data-browser-recording]');
    expect(bar?.hidden).toBe(false);
    expect(bar?.textContent).toContain('已录制 9 步');
    expect(bar?.querySelector('[data-browser-record="compile"]')).not.toBeNull();
    expect(bar?.querySelector('[data-browser-record="discard"]')).not.toBeNull();
  });

  it('生成技能只填输入框、不自动发送', async () => {
    // 轨迹进入模型上下文必须是用户按下发送键的那一刻。自动发送等于替用户
    // 做了那个决定——而这正是「录制不是接管的副作用」要守住的最后一环。
    document.body.innerHTML = `${renderBrowserPanel()}<textarea id="chat-input"></textarea>`;
    await activateRecordingSession('session-rec');
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValue(recordingResponse(true, false, 3));
    await sendRecordingControl('start');
    control.mockResolvedValue(recordingResponse(false, false, 0));
    await sendRecordingControl('stop');

    expect(compileLastRecording()).toBe(true);

    const input = document.querySelector<HTMLTextAreaElement>('#chat-input');
    expect(input?.value).toContain('编译成一个技能');
    expect(input?.value).toContain('录制 ID：abcd1234');
    expect(input?.value).toContain('共 3 步');
    // 路径是 owner 私有实现细节，不能进入 renderer 草稿或后续对话历史。
    expect(input?.value).not.toContain('/tmp/private-owner/trace');
    expect(input?.value).not.toContain('轨迹目录');
    // 入口用掉即收起，避免重复注入
    expect(document.querySelector('[data-browser-record="compile"]')).toBeNull();
  });

  it('已有草稿时追加而不是覆盖', async () => {
    document.body.innerHTML = `${renderBrowserPanel()}<textarea id="chat-input"></textarea>`;
    const input = document.querySelector<HTMLTextAreaElement>('#chat-input')!;
    input.value = '这个技能叫「查工单」';
    await activateRecordingSession('session-rec');
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValue(recordingResponse(true, false, 2));
    await sendRecordingControl('start');
    control.mockResolvedValue(recordingResponse(false, false, 0));
    await sendRecordingControl('stop');

    compileLastRecording();

    expect(input.value.startsWith('这个技能叫「查工单」')).toBe(true);
    expect(input.value).toContain('编译成一个技能');
  });

  it('生成技能只收起入口，绝不删轨迹', async () => {
    // 这是复核查出的主链路回归：填好提示词的同时把它指向的目录删了，用户按下
    // 发送时 Agent 读到的是一个不存在的路径。「用掉入口」与「删除数据」是两件事。
    document.body.innerHTML = `${renderBrowserPanel()}<textarea id="chat-input"></textarea>`;
    await activateRecordingSession('session-rec');
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValue(recordingResponse(true, false, 3));
    await sendRecordingControl('start');
    control.mockResolvedValue(recordingResponse(false, false, 0));
    await sendRecordingControl('stop');
    control.mockClear();

    expect(compileLastRecording()).toBe(true);

    // 入口收起
    expect(document.querySelector('[data-browser-record="compile"]')).toBeNull();
    // 但**没有**发出任何删除请求
    const discards = control.mock.calls.filter((call) => call[1] === 'record_discard');
    expect(discards).toHaveLength(0);
  });

  it('删除失败要如实告诉用户，不能照样说已丢弃', async () => {
    // 后端删不掉时返回的是 HTTP 200 + discarded:false（请求本身是成功的）。
    // 不看这个字段，用户会以为含真实业务数据的轨迹已经删了，而它还躺在盘上。
    document.body.innerHTML = `${renderBrowserPanel()}<textarea id="chat-input"></textarea>`;
    await activateRecordingSession('session-rec');
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValue(recordingResponse(true, false, 5));
    await sendRecordingControl('start');
    control.mockResolvedValue(recordingResponse(false, false, 0));
    await sendRecordingControl('stop');

    const notices: string[] = [];
    const stateModule = await import('../../src/ui/state');
    vi.spyOn(stateModule, 'notify').mockImplementation((text: string) => { notices.push(text); });

    control.mockResolvedValue({ ok: true, state: pageState(), discarded: false } as never);
    discardLastRecording();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(notices.some((text) => text.includes('未能删除'))).toBe(true);
  });

  it('丢弃后入口消失，且不再能编译', async () => {
    document.body.innerHTML = `${renderBrowserPanel()}<textarea id="chat-input"></textarea>`;
    await activateRecordingSession('session-rec');
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValue(recordingResponse(true, false, 5));
    await sendRecordingControl('start');
    control.mockResolvedValue(recordingResponse(false, false, 0));
    await sendRecordingControl('stop');

    discardLastRecording();

    expect(document.querySelector<HTMLElement>('[data-browser-recording]')?.hidden).toBe(true);
    expect(compileLastRecording()).toBe(false);
  });

  it('零步录制不留入口', async () => {
    // 点开录制又立刻停掉，不该弹出一个「生成技能」让用户去编译一份空轨迹
    document.body.innerHTML = renderBrowserPanel();
    await activateRecordingSession('session-rec');
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValue(recordingResponse(true, false, 0));
    await sendRecordingControl('start');
    control.mockResolvedValue(recordingResponse(false, false, 0));
    await sendRecordingControl('stop');

    expect(document.querySelector<HTMLElement>('[data-browser-recording]')?.hidden).toBe(true);
  });

  it('摘要把「要交出什么」摆在用户面前', async () => {
    // 轨迹记录的是用户真实看到的页面，交给 LLM 之前他有权知道走过哪些站点、
    // 有没有碰过密码框。这是知情，不是审批——不拦着他，只是不让他蒙着眼睛交。
    document.body.innerHTML = renderBrowserPanel();
    await activateRecordingSession('session-rec');
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValue(recordingResponse(true, false, 1));
    await sendRecordingControl('start');

    control.mockResolvedValue({
      ok: true,
      state: pageState(),
      recording: {
        recording: false, paused: false, steps: 0,
        recording_id: 'abcd1234',
        summary: {
          steps: 12, hosts: ['oa.hq.cmcc', 'sso.hq.cmcc'], notes: ['工单号每次不同'],
          masked_fields: 1, handoff_fields: 1, pages_captured: 3,
        },
      },
    });
    await sendRecordingControl('stop');

    const bar = document.querySelector<HTMLElement>('[data-browser-recording]');
    // 步数以摘要里的实际落盘条数为准，不是宿主计数
    expect(bar?.textContent).toContain('已录制 12 步');
    expect(bar?.textContent).toContain('oa.hq.cmcc');
    // **如实说明轨迹里有密码原值。** 早先这里断言的是「已屏蔽」，而当前
    // recorder schema 起分级只是描述性元数据、值一个不少地落盘——用户正是在
    // 这一屏决定要不要把轨迹交出去，一句假话会让他做出相反的决定。
    expect(bar?.textContent).toContain('含 1 处密码原值');
    expect(bar?.textContent).not.toContain('已屏蔽');
    // 验证码那档说「需人工」是准确的：编译期强制转成人工接管。
    expect(bar?.textContent).toContain('1 处验证码需人工');
    expect(bar?.textContent).toContain('1 条标注');
  });

  it('录制中提供标注按钮，未录制时不提供', async () => {
    document.body.innerHTML = renderBrowserPanel();
    await activateRecordingSession('session-rec');
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValue(recordingResponse(true, false, 0));
    await sendRecordingControl('start');
    expect(document.querySelector('[data-browser-record="note"]')).not.toBeNull();

    expect(await addRecordingNote('  这个工单号每次都不同  ')).toBe(true);
    expect(control).toHaveBeenLastCalledWith(
      'session-rec', 'record_note', '这个工单号每次都不同',
    );

    control.mockResolvedValue(recordingResponse(false, false, 0));
    await sendRecordingControl('stop');
    // 没在录制就不该能加标注
    expect(await addRecordingNote('迟到的标注')).toBe(false);
  });

  it('空标注不发请求', async () => {
    document.body.innerHTML = renderBrowserPanel();
    await activateRecordingSession('session-rec');
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValue(recordingResponse(true, false, 0));
    await sendRecordingControl('start');
    control.mockClear();

    expect(await addRecordingNote('   ')).toBe(false);
    expect(control).not.toHaveBeenCalled();
  });

  it('工具栏提供开始录制入口', async () => {
    // 这是审查查出的最致命的 P0：生产 UI 里根本没有开始录制的按钮 ——
    // 录制条只在**已经在录制**时才渲染，而事件处理只覆盖 pause/resume/stop。
    // 功能在真实 UI 上无法启动，之前的「端到端验证」是探针绕开 UI 跑的假阳性。
    //
    // 注意 renderBrowserPanel() 读的是模块内的 pageState，不接受参数——所以
    // 必须先让面板真的拿到一个页面状态，不能只把 state 传进渲染函数。
    document.body.innerHTML = renderBrowserPanel();
    // 空白页也**不禁用**：禁用按钮在 Chromium 里连原生提示都不弹，用户只会看到
    // 一个点不动、也不说为什么的死胡同。空白页按下改为进入预备态（见下一条用例）。
    expect(document.querySelector<HTMLButtonElement>('[data-browser-record="start"]')?.disabled)
      .toBe(false);

    setActiveSessionId('session-entry');
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({
      ok: true,
      state: pageState({ tab_id: 'tab-1', url: 'https://example.com/', mode: 'human' }),
    });
    await openUserBrowser();
    document.body.innerHTML = renderBrowserPanel();

    const start = document.querySelector<HTMLButtonElement>('[data-browser-record="start"]');
    expect(start).not.toBeNull();
    expect(start?.disabled).toBe(false);
    expect(start?.getAttribute('aria-label')).toBe('开始录制技能');
    // 悬停必须能看懂这个点是干嘛的。注意：Chromium 对 disabled 元素不派发鼠标
    // 事件，原生 title 不会弹——所以「不禁用」同时也是提示能否出现的前提。
    expect(start?.getAttribute('title')).toContain('录制');
  });

  it('空白页按下录制进入预备态，打开网页后自动开始', async () => {
    // 「先按录制、再打开网站」才是自然顺序（Playwright codegen 就是这样）。
    // 但空白页真开录必然失败：宿主合成的第一步 openPage 需要视口，而空白标签页
    // 的原生 view 是分离的，没有视口 → 整段被标 incomplete。所以先预备、
    // 等真实页面挂上再开——用户体感是"按下就开始"，轨迹侧拿到的仍是正常的 openPage。
    document.body.innerHTML = renderBrowserPanel();
    setActiveSessionId('session-armed');
    const control = vi.spyOn(backendApi, 'browserControl');

    await sendRecordingControl('start');

    // 预备态不能谎称正在录制
    expect(browserRecordingState().recording).toBe(false);
    expect(control).not.toHaveBeenCalled();
    const armed = document.querySelector<HTMLElement>('[data-browser-rec-controls] .browser-rec');
    expect(armed?.classList.contains('is-armed')).toBe(true);
    expect(armed?.textContent).toContain('预备录制');

    // 第一个真实页面挂上 → 自动开录
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({
      ok: true,
      state: pageState({ tab_id: 'tab-1', url: 'https://example.com/', mode: 'human' }),
    });
    control.mockResolvedValue(recordingResponse(true, false, 1));

    await openUserBrowser();
    await vi.waitFor(() => expect(browserRecordingState().recording).toBe(true));

    expect(control).toHaveBeenCalledWith('session-armed', 'record_start');
    expect(document.querySelector('[data-browser-rec-controls] .browser-rec.is-armed')).toBeNull();
  });

  it('标注按钮打开内联输入框，而不是 Electron 不支持的 window.prompt', async () => {
    // window.prompt 在 Electron 里根本不实现（wiki-page 也踩过并留了注释），
    // 此前标注按钮就是 prompt——点了永远没反应。这条钉住它必须给出真的输入口。
    document.body.innerHTML = renderBrowserPanel();
    await activateRecordingSession('session-note');
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValue(recordingResponse(true, false, 3));
    await sendRecordingControl('start');

    openRecordingNoteComposer();

    const form = document.querySelector<HTMLElement>('[data-browser-note]');
    const input = document.querySelector<HTMLInputElement>('[data-browser-note-input]');
    expect(form?.hidden).toBe(false);
    expect(input).not.toBeNull();
    // 借用地址栏那一格，所以地址栏这时让位——录制 UI 一行都不占
    expect(document.querySelector<HTMLElement>('[data-browser-url]')?.hidden).toBe(true);

    input!.value = '这个工单号每次都不同';
    expect(await addRecordingNote(input!.value)).toBe(true);
    expect(control).toHaveBeenCalledWith('session-note', 'record_note', '这个工单号每次都不同');

    closeRecordingNoteComposer();
    expect(document.querySelector<HTMLElement>('[data-browser-note]')?.hidden).toBe(true);
    expect(document.querySelector<HTMLElement>('[data-browser-url]')?.hidden).toBe(false);
  });

  it('录制态按会话隔离，B 会话看不到也停不掉 A 的录制', async () => {
    // 早先 recordingState / lastRecording 是跨会话的全局变量：切到会话 B
    // 会看到 A 的录制红点，点「停止」停的是 A 的录制。
    document.body.innerHTML = renderBrowserPanel();
    await activateRecordingSession('session-a');
    vi.spyOn(backendApi, 'browserControl').mockResolvedValue(recordingResponse(true, false, 5));
    await sendRecordingControl('start');
    expect(browserRecordingState()).toMatchObject({ recording: true, steps: 5 });

    // 切到 B：不该看到 A 的录制态
    setActiveSessionId('session-b');
    expect(browserRecordingState().recording).toBe(false);
    renderBrowserRecordingBar();
    expect(document.querySelector<HTMLElement>('[data-browser-recording]')?.hidden).toBe(true);

    // 切回 A：状态还在
    setActiveSessionId('session-a');
    expect(browserRecordingState()).toMatchObject({ recording: true, steps: 5 });
  });

  it('没有活动会话时不发请求', async () => {
    document.body.innerHTML = renderBrowserPanel();
    setActiveSessionId(null);
    const control = vi.spyOn(backendApi, 'browserControl');

    await sendRecordingControl('start');

    expect(control).not.toHaveBeenCalled();
  });
});
