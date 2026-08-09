/**
 * security-approval UI 绑定测试（U2）：验证「始终允许」需要 window.confirm 二次确认。
 * @vitest-environment happy-dom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Mock chat-controller：security-approval.ts import 了 appendMessage / renderChat，
// 真实模块会拉入大量 DOM 依赖，这里用 no-op mock 隔离。
vi.mock('../../src/ui/features/chat-controller', () => ({
  appendMessage: vi.fn(),
  renderChat: vi.fn(),
}));

// Mock state：提供最小可用的 activeSessionId / sessions，避免拉入真实 store。
vi.mock('../../src/ui/state', () => ({
  $: (selector: string) => document.querySelector(selector),
  $$: (selector: string) => Array.from(document.querySelectorAll(selector)),
  notify: vi.fn(),
  state: {
    activeSessionId: 's1',
    sessions: [{ id: 's1', workspaceId: 'default' }],
    currentWorkspaceId: 'default',
  },
}));

import { bindSecurityApprovalUi } from '../../src/ui/features/security-approval';

type MockCrew = {
  securityPending: ReturnType<typeof vi.fn>;
  securityDecide: ReturnType<typeof vi.fn>;
};

function setupDom(): void {
  document.body.innerHTML = `
    <div class="chat-input-container">
      <div id="composer-approval-panel" aria-hidden="true">
        <pre id="composer-approval-summary"></pre>
        <div class="composer-approval-panel__actions">
          <button class="btn btn-danger" type="button" data-security-decision="reject">拒绝</button>
          <button class="btn btn-primary" type="button" data-security-decision="once">仅这一次</button>
          <button class="btn btn-ghost" type="button" data-security-decision="session">本次对话</button>
          <button class="btn btn-ghost" type="button" data-security-decision="always">始终允许</button>
        </div>
      </div>
    </div>
  `;
}

function mockCrew(): MockCrew {
  const mw: MockCrew = {
    securityPending: vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body: {
        requests: [
          { request_id: 'r1', task_id: 't1', workspace_id: 'default', action: { argv: ['ls'] } },
        ],
      },
    }),
    securityDecide: vi.fn().mockResolvedValue({ ok: true, status: 200, body: {} }),
  };
  (window as unknown as { Crew: unknown }).Crew = mw;
  return mw;
}

describe('security approval always-confirm (U2)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('confirm', vi.fn());
    setupDom();
    mockCrew();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('requires window.confirm before always decision', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const cleanup = bindSecurityApprovalUi();
    // 触发 1s 轮询拉取 pending 请求
    await vi.advanceTimersByTimeAsync(1000);
    // 点击 always 按钮
    const alwaysBtn = document.querySelector('[data-security-decision="always"]') as HTMLButtonElement;
    alwaysBtn.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    // confirm 返回 false -> 不应调用 securityDecide
    const mw = (window as unknown as { Crew: MockCrew }).Crew;
    expect(mw.securityDecide).not.toHaveBeenCalled();
    cleanup();
  });

  it('proceeds with always when confirm returns true', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const cleanup = bindSecurityApprovalUi();
    await vi.advanceTimersByTimeAsync(1000);
    const alwaysBtn = document.querySelector('[data-security-decision="always"]') as HTMLButtonElement;
    alwaysBtn.click();
    await vi.advanceTimersByTimeAsync(0);
    const mw = (window as unknown as { Crew: MockCrew }).Crew;
    expect(mw.securityDecide).toHaveBeenCalledWith(expect.objectContaining({ decision: 'always' }));
    cleanup();
  });

  it('does not prompt confirm for once decision', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const cleanup = bindSecurityApprovalUi();
    await vi.advanceTimersByTimeAsync(1000);
    const onceBtn = document.querySelector('[data-security-decision="once"]') as HTMLButtonElement;
    onceBtn.click();
    await vi.advanceTimersByTimeAsync(0);
    // once 是最保守的批准，不需要二次确认
    expect(confirmSpy).not.toHaveBeenCalled();
    cleanup();
  });

  it('omits alwaysArgvPrefix for file actions (empty argv) so IPC does not reject', async () => {
    // 回归：文件/网络审批的 action 无 argv，历史实现仍发空 alwaysArgvPrefix，被 IPC schema
    // 判为非法并 reject，导致「始终允许」点了没反应。修复后 argv 为空时不携带该字段。
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const mw = (window as unknown as { Crew: MockCrew }).Crew;
    mw.securityPending.mockResolvedValue({
      ok: true,
      status: 200,
      body: {
        requests: [
          {
            request_id: 'rf',
            task_id: 'tf',
            workspace_id: 'default',
            action: { kind: 'file', path: '/tmp/x.txt', operation: 'write' },
          },
        ],
      },
    });
    const cleanup = bindSecurityApprovalUi();
    await vi.advanceTimersByTimeAsync(1000);
    const alwaysBtn = document.querySelector('[data-security-decision="always"]') as HTMLButtonElement;
    alwaysBtn.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(mw.securityDecide).toHaveBeenCalledTimes(1);
    const arg = mw.securityDecide.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(arg).toMatchObject({ decision: 'always' });
    expect(arg).not.toHaveProperty('alwaysArgvPrefix');
    cleanup();
  });

  it('recovers overlay when securityDecide rejects instead of hanging silently', async () => {
    // 回归：securityDecide 走 IPC，reject 时旧代码无 try/catch → overlay 不关、工具挂到超时。
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const mw = (window as unknown as { Crew: MockCrew }).Crew;
    mw.securityDecide.mockRejectedValue(new Error('bad prefix'));
    const cleanup = bindSecurityApprovalUi();
    await vi.advanceTimersByTimeAsync(1000);
    const panel = document.querySelector('#composer-approval-panel') as HTMLElement;
    expect(panel.getAttribute('aria-hidden')).toBe('false');
    const alwaysBtn = document.querySelector('[data-security-decision="always"]') as HTMLButtonElement;
    alwaysBtn.click();
    await vi.advanceTimersByTimeAsync(0);
    // 异常被兜底 catch：overlay 撤掉，不再静默卡死。
    expect(panel.getAttribute('aria-hidden')).toBe('true');
    cleanup();
  });

  it('submits only one decision while the gateway acknowledgement is pending', async () => {
    const mw = (window as unknown as { Crew: MockCrew }).Crew;
    let acknowledge: ((value: { ok: true; status: 200; body: object }) => void) | undefined;
    mw.securityDecide.mockImplementation(() => new Promise((resolve) => {
      acknowledge = resolve;
    }));
    const cleanup = bindSecurityApprovalUi();
    await vi.advanceTimersByTimeAsync(1000);

    const once = document.querySelector('[data-security-decision="once"]') as HTMLButtonElement;
    const reject = document.querySelector('[data-security-decision="reject"]') as HTMLButtonElement;
    once.click();
    reject.click();
    await vi.advanceTimersByTimeAsync(0);

    expect(mw.securityDecide).toHaveBeenCalledTimes(1);
    expect(once.disabled).toBe(true);
    expect(reject.disabled).toBe(true);
    acknowledge?.({ ok: true, status: 200, body: {} });
    await vi.advanceTimersByTimeAsync(0);
    cleanup();
  });

  it('polls immediately when the gateway pushes an approval wake-up', async () => {
    const mw = (window as unknown as { Crew: MockCrew }).Crew;
    const cleanup = bindSecurityApprovalUi();

    window.dispatchEvent(new CustomEvent('security:approval-pending'));
    await vi.advanceTimersByTimeAsync(0);

    expect(mw.securityPending).toHaveBeenCalledOnce();
    cleanup();
  });
});
