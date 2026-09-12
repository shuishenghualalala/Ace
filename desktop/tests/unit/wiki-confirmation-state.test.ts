// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { backendApi } from '../../src/ui/backend-client';
import { dismissWikiConfirmation, revealPendingWikiConfirmation } from '../../src/ui/features/wiki-confirmation-state';

afterEach(() => vi.restoreAllMocks());

describe('Wiki 历史审批恢复', () => {
  it('仅后端仍待确认时打开，重新进入时已消费的确认保持隐藏', async () => {
    vi.spyOn(backendApi, 'wikiConfirmationStatus')
      .mockResolvedValueOnce({ pending: true })
      .mockResolvedValueOnce({ pending: false });
    const first = document.createElement('div');
    first.setAttribute('aria-hidden', 'true');
    await revealPendingWikiConfirmation('pending', first);
    expect(first.getAttribute('aria-hidden')).toBe('false');
    const restored = document.createElement('div');
    restored.setAttribute('aria-hidden', 'true');
    await revealPendingWikiConfirmation('pending', restored);
    expect(restored.getAttribute('aria-hidden')).toBe('true');
  });

  it('点击后迟到的状态查询不能重新打开审批', async () => {
    let resolve!: (value: { pending: boolean }) => void;
    vi.spyOn(backendApi, 'wikiConfirmationStatus').mockReturnValue(new Promise((done) => { resolve = done; }));
    const element = document.createElement('div');
    element.setAttribute('aria-hidden', 'true');
    const query = revealPendingWikiConfirmation('clicked', element);
    dismissWikiConfirmation('clicked');
    resolve({ pending: true });
    await query;
    expect(element.getAttribute('aria-hidden')).toBe('true');
  });
});
