/**
 * Mermaid 图表渲染：扫描容器内的 `[data-mermaid]` 占位，懒加载 mermaid.js 替换成 SVG。
 *
 * 设计要点：
 *  - 真正懒加载：mermaid.js 体积大（min.js ~3.3mb），不打进 renderer bundle。只在 DOM 出现
 *    mermaid 占位时才动态创建 `<script src="./mermaid.min.js">` 加载，避免拖慢无图表会话的首屏。
 *    esbuild.config.mjs 把 mermaid 列为 external 并单独 copyFile 到 dist/assets/mermaid.min.js。
 *  - 幂等：用 `data-mermaid-rendered` 标记已处理节点，重复调用只处理新增占位。
 *    chat-controller 每次 patch/reuse 后调用，已渲染的节点跳过。
 *  - 容错：mermaid.run 可能因「源码未闭合/语法错误」失败（流式中半截图很常见）。
 *    失败时标记 `data-mermaid-error`，保留源码占位；下次 patch 重建干净占位后会重新尝试，
 *    等流式结束、源码完整时自然渲染成功。
 *  - 安全：mermaid 库本身保证输出 SVG 无脚本注入；占位 div 经过 DOMPurify 白名单
 *    （data-mermaid 属性已放行），源码是 micromark escaped 的，textContent 自动 unescape 给 mermaid。
 *  - 主题：按当前 prefers-color-scheme 选 dark/light。桌面端切主题后已渲染图不自动重渲，
 *    需切会话回来触发重建——已知限制。
 */

/** Mermaid API 的类型（type-only import，不产生运行时依赖，由 script 提供 window.mermaid）。 */
type MermaidAPI = typeof import('mermaid')['default'];

declare global {
  interface Window {
    mermaid?: MermaidAPI;
  }
}

/** script 加载的 Promise 缓存（首次加载后复用，避免重复创建 script 标签）。 */
let mermaidPromise: Promise<MermaidAPI> | null = null;

function loadMermaid(): Promise<MermaidAPI> {
  if (mermaidPromise) return mermaidPromise;
  // 已加载过（其它路径注入了 window.mermaid）则直接复用。
  if (typeof window !== 'undefined' && window.mermaid) {
    mermaidPromise = Promise.resolve(window.mermaid);
    return mermaidPromise;
  }
  mermaidPromise = new Promise<MermaidAPI>((resolve, reject) => {
    const script = document.createElement('script');
    script.src = './mermaid.min.js';
    script.async = true;
    script.onload = () => {
      const m = window.mermaid;
      if (!m) {
        reject(new Error('mermaid script loaded but window.mermaid missing'));
        return;
      }
      const prefersDark =
        window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
      m.initialize({
        startOnLoad: false,
        theme: prefersDark ? 'dark' : 'default',
        securityLevel: 'strict', // 禁止 mermaid 输出 HTML/脚本，只产 SVG
      });
      resolve(m);
    };
    script.onerror = () => reject(new Error('mermaid script load failed'));
    document.head.appendChild(script);
  });
  return mermaidPromise;
}

/**
 * 渲染 root 内所有未处理的 mermaid 占位。
 *
 * @param root 某个消息节点或 document；只扫描其内部未渲染的 [data-mermaid]。
 */
export async function renderMermaidBlocks(root: HTMLElement | Document): Promise<void> {
  const placeholders = root.querySelectorAll<HTMLElement>(
    '[data-mermaid]:not([data-mermaid-rendered]):not([data-mermaid-error])',
  );
  if (placeholders.length === 0) return;
  let mermaid: MermaidAPI;
  try {
    mermaid = await loadMermaid();
  } catch {
    // mermaid 加载失败（网络/打包问题）：标记所有占位为 error，避免反复尝试。
    placeholders.forEach((el) => el.setAttribute('data-mermaid-error', 'load-failed'));
    return;
  }
  const nodes = Array.from(placeholders);
  // 先标记 rendered，防止下次重复处理；失败的额外回退标记。
  nodes.forEach((el) => el.setAttribute('data-mermaid-rendered', '1'));
  try {
    await mermaid.run({ nodes });
  } catch (err) {
    // run 对部分节点失败会整体抛错。把「内容不是 SVG」的节点回退成源码占位、标 error，
    // 等下次 patch 重建干净占位后重试（流式半截图场景的关键容错）。
    nodes.forEach((el) => {
      if (!el.querySelector('svg')) {
        el.removeAttribute('data-mermaid-rendered');
        el.setAttribute('data-mermaid-error', 'render-failed');
      }
    });
    void err;
  }
}

/**
 * 重置 mermaid 加载状态。用于主题切换等需要重新初始化的场景。
 * 注意：已渲染的 SVG 不会自动重渲染，需要调用方重新触发 renderMermaidBlocks。
 */
export function resetMermaidLoader(): void {
  mermaidPromise = null;
}
