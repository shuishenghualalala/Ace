/**
 * copy-button 单测：按钮绑定、复制文本定位、反馈、幂等。
 * @vitest-environment happy-dom
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { attachCopyButtons } from '../../src/ui/features/copy-button';

function makeWrapper(code: string, lang = 'js'): HTMLElement {
  const wrapper = document.createElement('div');
  wrapper.className = 'code-block-wrapper';
  wrapper.innerHTML = `
    <div class="code-block-header">
      <span class="code-block-lang">${lang}</span>
      <button class="code-block-copy" data-copy type="button">复制</button>
    </div>
    <pre class="chat-md-code"><code>${code}</code></pre>
  `;
  return wrapper;
}

describe('attachCopyButtons', () => {
  beforeEach(() => {
    // mock clipboard
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
    });
  });

  it('点击按钮复制对应 code 文本', async () => {
    const root = document.createElement('div');
    root.appendChild(makeWrapper('console.log(1)'));
    attachCopyButtons(root);
    const btn = root.querySelector('button[data-copy]') as HTMLButtonElement;
    btn.click();
    // 等微任务
    await new Promise((r) => setTimeout(r, 0));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('console.log(1)');
  });

  it('幂等：重复调用不重复绑定', async () => {
    const root = document.createElement('div');
    root.appendChild(makeWrapper('a'));
    attachCopyButtons(root);
    attachCopyButtons(root);
    const btn = root.querySelector('button[data-copy]') as HTMLButtonElement;
    btn.click();
    await new Promise((r) => setTimeout(r, 0));
    expect((navigator.clipboard.writeText as unknown as { mock: { calls: unknown[][] } }).mock.calls.length).toBe(1);
  });

  it('反馈：复制成功后按钮文字短暂变「已复制」', async () => {
    vi.useFakeTimers();
    const root = document.createElement('div');
    root.appendChild(makeWrapper('x'));
    attachCopyButtons(root);
    const btn = root.querySelector('button[data-copy]') as HTMLButtonElement;
    btn.click();
    await vi.advanceTimersByTimeAsync(0);
    expect(btn.textContent).toBe('已复制');
    vi.advanceTimersByTime(1300);
    expect(btn.textContent).toBe('复制');
    vi.useRealTimers();
  });

  it('无 code-block-wrapper 时按钮不抛错', () => {
    const root = document.createElement('div');
    const btn = document.createElement('button');
    btn.setAttribute('data-copy', '');
    root.appendChild(btn);
    expect(() => attachCopyButtons(root)).not.toThrow();
    expect(() => btn.click()).not.toThrow();
  });
});
