/** browser-auto-open：Agent 首个 browser_use 动作自动展开浏览器面板的判定单测。 */
import { describe, it, expect } from 'vitest';
import { shouldAutoOpenBrowserWorkbench } from '../../src/ui/features/browser-auto-open';

const base = {
  kind: 'tool',
  toolName: 'browser_use',
  sessionId: 'sid-1',
  activeSessionId: 'sid-1',
  requestId: 'req-1',
};

describe('shouldAutoOpenBrowserWorkbench', () => {
  it('首个 browser_use 动作自动展开一次', () => {
    expect(shouldAutoOpenBrowserWorkbench({ ...base })).toBe(true);
  });

  it('同一 request 不重复展开（用户手动关闭后不再打扰）', () => {
    expect(
      shouldAutoOpenBrowserWorkbench({ ...base, lastOpenedRequestId: 'req-1' }),
    ).toBe(false);
  });

  it('新一轮 request 再次允许自动展开', () => {
    expect(
      shouldAutoOpenBrowserWorkbench({ ...base, lastOpenedRequestId: 'req-0' }),
    ).toBe(true);
  });

  it('非 browser_use 工具 / 非 tool chunk 不触发', () => {
    expect(shouldAutoOpenBrowserWorkbench({ ...base, toolName: 'terminal' })).toBe(false);
    expect(shouldAutoOpenBrowserWorkbench({ ...base, kind: 'status', toolName: undefined })).toBe(false);
  });

  it('后台会话不打扰当前界面', () => {
    expect(
      shouldAutoOpenBrowserWorkbench({ ...base, activeSessionId: 'sid-2' }),
    ).toBe(false);
    expect(
      shouldAutoOpenBrowserWorkbench({ ...base, activeSessionId: '' }),
    ).toBe(false);
  });

  it('无法识别轮次（无 requestId）时退化为不自动展开', () => {
    expect(shouldAutoOpenBrowserWorkbench({ ...base, requestId: null })).toBe(false);
  });
});
