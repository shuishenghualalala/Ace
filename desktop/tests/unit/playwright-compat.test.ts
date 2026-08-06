import { describe, expect, it, vi } from 'vitest';

import {
  enableFocusEmulation,
  officialInjectedScriptSource,
} from '../../src/main/browser/playwright-compat';

describe('playwright-compat CDP session lifecycle', () => {
  it('extracts the installed official InjectedScript deterministically', () => {
    const first = officialInjectedScriptSource();
    const second = officialInjectedScriptSource();

    expect(second).toBe(first);
    expect(first.version).toMatch(/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/);
    expect(first.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(first.source.length).toBeGreaterThan(100_000);
    expect(first.source).toContain('generateSelectorSimple(');
    expect(first.source).toContain('InjectedScript: () => InjectedScript');
  });

  it('enableFocusEmulation 成功后 finally detach', async () => {
    const send = vi.fn(async () => undefined);
    const detach = vi.fn(async () => undefined);
    const context = {
      newCDPSession: vi.fn(async () => ({ send, detach })),
    };
    const page = {};

    await enableFocusEmulation(context as never, page as never);

    expect(send).toHaveBeenCalledWith('Emulation.setFocusEmulationEnabled', { enabled: true });
    expect(detach).toHaveBeenCalledTimes(1);
  });

  it('enableFocusEmulation 发送失败也 finally detach，保留原始错误', async () => {
    const failure = new Error('focus command failed');
    const send = vi.fn(async () => {
      throw failure;
    });
    const detach = vi.fn(async () => undefined);
    const context = {
      newCDPSession: vi.fn(async () => ({ send, detach })),
    };

    await expect(enableFocusEmulation(context as never, {} as never)).rejects.toBe(failure);
    expect(detach).toHaveBeenCalledTimes(1);
  });
});
