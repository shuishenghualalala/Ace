/**
 * mermaid-render 单测：幂等、容错、script 创建逻辑。
 *
 * 不验证真实 script 加载（浏览器原语，happy-dom 不支持）——预设 window.mermaid
 * 跳过加载路径，只验证渲染/容错；另单独验证「无 window.mermaid 时创建 script 且 src 正确」。
 * @vitest-environment happy-dom
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

interface MockMermaid {
  run: ReturnType<typeof vi.fn>;
  initialize: ReturnType<typeof vi.fn>;
}

let resetMermaidLoader: () => void;

beforeEach(async () => {
  const mod = await import('../../src/ui/features/mermaid-render');
  resetMermaidLoader = mod.resetMermaidLoader;
  resetMermaidLoader();
  delete (window as unknown as { mermaid?: MockMermaid }).mermaid;
  document.querySelectorAll('script').forEach((s) => s.remove());
});

function setMockMermaid(overrides: Partial<MockMermaid> = {}): MockMermaid {
  const m: MockMermaid = {
    run: vi.fn().mockResolvedValue(undefined),
    initialize: vi.fn(),
    ...overrides,
  };
  (window as unknown as { mermaid?: MockMermaid }).mermaid = m;
  return m;
}

describe('renderMermaidBlocks', () => {
  it('无占位时不加载 mermaid（不创建 script、不调用 initialize）', async () => {
    const { renderMermaidBlocks } = await import('../../src/ui/features/mermaid-render');
    const m = setMockMermaid();
    const root = document.createElement('div');
    root.innerHTML = '<p>无图</p>';
    await renderMermaidBlocks(root);
    expect(m.run).not.toHaveBeenCalled();
    expect(m.initialize).not.toHaveBeenCalled();
  });

  it('有占位时调用 run 并标记 rendered', async () => {
    const { renderMermaidBlocks } = await import('../../src/ui/features/mermaid-render');
    const m = setMockMermaid();
    const root = document.createElement('div');
    root.innerHTML = '<div class="mermaid" data-mermaid>graph TD\nA-->B</div>';
    await renderMermaidBlocks(root);
    // 预设 window.mermaid 走快路径，不重复 initialize（避免覆盖外部初始化）。
    expect(m.run).toHaveBeenCalled();
    expect(root.querySelector('[data-mermaid]')?.getAttribute('data-mermaid-rendered')).toBe('1');
  });

  it('幂等：已 rendered 的占位不重复处理', async () => {
    const { renderMermaidBlocks } = await import('../../src/ui/features/mermaid-render');
    const m = setMockMermaid();
    const root = document.createElement('div');
    root.innerHTML = '<div class="mermaid" data-mermaid data-mermaid-rendered="1">x</div>';
    await renderMermaidBlocks(root);
    expect(m.run).not.toHaveBeenCalled();
  });

  it('容错：mermaid.run 抛错时标 error 且移除 rendered（保留源码占位待重试）', async () => {
    const { renderMermaidBlocks } = await import('../../src/ui/features/mermaid-render');
    setMockMermaid({ run: vi.fn().mockRejectedValue(new Error('parse error')) });
    const root = document.createElement('div');
    root.innerHTML = '<div class="mermaid" data-mermaid>bad source</div>';
    await renderMermaidBlocks(root);
    const el = root.querySelector('[data-mermaid]') as HTMLElement;
    expect(el.getAttribute('data-mermaid-error')).toBe('render-failed');
    expect(el.hasAttribute('data-mermaid-rendered')).toBe(false);
    expect(el.textContent).toContain('bad source');
  });

  it('window.mermaid 不存在时创建 script 且 src 指向 ./mermaid.min.js', async () => {
    const { renderMermaidBlocks } = await import('../../src/ui/features/mermaid-render');
    // 不预设 window.mermaid → 走 script 加载路径。
    // 用 spy 拦截 appendChild，阻止 happy-dom 真实加载，只验证 script.src。
    const appendSpy = vi.spyOn(document.head, 'appendChild').mockImplementation((node) => {
      const script = node as HTMLScriptElement;
      expect(script.src).toContain('mermaid.min.js');
      // 不真正 append，避免 happy-dom 触发 loadScript 抛错。
      return node;
    });
    const root = document.createElement('div');
    root.innerHTML = '<div class="mermaid" data-mermaid>graph TD\nA-->B</div>';
    // 不 await：loadMermaid 的 promise 会 hang 在 onload（我们没触发），但 script.src 已验证。
    void renderMermaidBlocks(root);
    // 给微任务一点时间让 loadMermaid 创建 script。
    await new Promise((r) => setTimeout(r, 0));
    expect(appendSpy).toHaveBeenCalled();
    appendSpy.mockRestore();
  });
});
